#!/usr/bin/env python3
"""采集新浪公开分红表，并保存带结构和覆盖边界的公司行动证据。"""
from __future__ import annotations

import argparse
import asyncio
import html as html_lib
from pathlib import Path
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.core.store import store

TZ = ZoneInfo('Asia/Shanghai')
PARSE_VERSION = 'corp_actions_table_v1_1'
SOURCE_NAME = 'sina_sharebonus_v1_1'
HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
TABLE_RE = re.compile(r'<table\b[^>]*>(.*?)</table>', re.I | re.S)
ROW_RE = re.compile(r'<tr\b[^>]*>(.*?)</tr>', re.I | re.S)
CELL_RE = re.compile(r'<t[hd]\b[^>]*>(.*?)</t[hd]>', re.I | re.S)
TAG_RE = re.compile(r'<[^>]+>')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
CODE_RE = re.compile(r'^\d{6}$')


def universe_codes() -> list[str]:
    index = store.kv_get('ml_r1:universe:index', []) or []
    for key in sorted(index, reverse=True):
        rec = store.kv_get(key)
        if rec and rec.get('stocks'):
            return sorted({s['code'] for s in rec['stocks']})
    return []


def _text(value: str) -> str:
    return re.sub(r'\s+', ' ', html_lib.unescape(TAG_RE.sub(' ', value))).strip()


def _num(text: str) -> float | None:
    text = text.replace(',', '').strip()
    if not text or text in {'-', '--', '—', '－', 'N/A'}:
        return None
    match = re.search(r'-?\d+(?:\.\d+)?', text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _column(headers: list[str], token: str) -> int | None:
    return next((i for i, value in enumerate(headers) if token in value), None)


def _parse_table(html: str) -> tuple[list[dict], dict]:
    required = ('送股', '转增', '派息', '进度', '除权除息日', '股权登记日')
    for table_html in TABLE_RE.findall(html):
        rows = [[_text(c) for c in CELL_RE.findall(row)] for row in ROW_RE.findall(table_html)]
        if not rows:
            continue
        headers = rows[0]
        if not all(_column(headers, token) is not None for token in required):
            continue
        indexes = {token: _column(headers, token) for token in required}
        events, invalid, data_rows = [], 0, 0
        dates = []
        for cells in rows[1:]:
            if not any(cells):
                continue
            data_rows += 1
            if len(cells) <= max(i for i in indexes.values() if i is not None):
                invalid += 1
                continue
            status = cells[indexes['进度']]
            ex_date = cells[indexes['除权除息日']]
            bonus10 = _num(cells[indexes['送股']])
            convert10 = _num(cells[indexes['转增']])
            cash10 = _num(cells[indexes['派息']])
            record_date = cells[indexes['股权登记日']]
            if status != '实施':
                continue
            if not DATE_RE.match(ex_date) or not DATE_RE.match(record_date) or any(
                    value is None for value in (bonus10, convert10, cash10)):
                invalid += 1
                continue
            bonus, convert, cash = bonus10 / 10.0, convert10 / 10.0, cash10 / 10.0
            if cash > 0 and bonus == 0 and convert == 0:
                kind = 'cash'
            elif cash == 0 and bonus + convert > 0:
                kind = 'split'
            else:
                kind = 'complex'
            dates.append(ex_date)
            events.append({'announce_date': cells[0] if DATE_RE.match(cells[0]) else None,
                           'ex_date': ex_date, 'record_date': record_date,
                           'bonus_ps': bonus, 'convert_ps': convert, 'cash_ps': cash,
                           'kind': kind, 'verified': True, 'complete': True})
        return events, {'table_found': True, 'table_rows': data_rows, 'valid_rows': len(events),
                        'invalid_rows': invalid, 'parse_complete': invalid == 0,
                        'start_date': min(dates) if dates else None,
                        'end_date': max(dates) if dates else None}
    return [], {'table_found': False, 'table_rows': 0, 'valid_rows': 0,
                'invalid_rows': 0, 'parse_complete': False,
                'start_date': None, 'end_date': None}


def parse_events(html: str) -> list[dict]:
    """解析明确的新浪分红表；其它页面结构一律返回空列表。"""
    return _parse_table(html)[0]


def parse_page(html: str, *, code: str, source_url: str) -> dict:
    events, quality = _parse_table(html)
    code_ok = bool(CODE_RE.match(code))
    parse_complete = bool(code_ok and quality['table_found'] and quality['parse_complete'])
    if not code_ok:
        status = 'invalid_code'
    elif not quality['table_found']:
        status = 'missing_share_bonus_table'
    elif not events:
        status = 'empty_share_bonus_table'
    elif not parse_complete:
        status = 'incomplete_share_bonus_table'
    else:
        status = 'ok'
    return {'code': code, 'source': SOURCE_NAME, 'source_url': source_url,
            'parse_version': PARSE_VERSION, 'status': status,
            'verified': parse_complete and bool(events), 'complete': parse_complete,
            'all_corporate_actions': False,
            'coverage': {'scope': 'share_bonus_table', 'all_corporate_actions': False,
                         **quality}, 'events': events}


async def one(client: httpx.AsyncClient, sem: asyncio.Semaphore, code: str,
              stats: dict, *, dry_run: bool = False) -> None:
    url = f'http://money.finance.sina.com.cn/corp/go.php/vISSUE_ShareBonus/stockid/{code}.phtml'
    async with sem:
        for attempt in range(3):
            stats['requests'] += 1
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    page = parse_page(response.text, code=code, source_url=url)
                    if page['status'] in {'ok', 'empty_share_bonus_table'}:
                        if page['status'] == 'empty_share_bonus_table':
                            stats['empty_table'] += 1
                        if not dry_run:
                            page['collected_at'] = datetime.now(TZ).isoformat()
                            store.kv_set('corp_actions:' + code, page)
                        stats['ok'] += 1
                        stats['events'] += len(page['events'])
                        return
                    stats['invalid_page'] += 1
                else:
                    stats['bad_status'] += 1
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1.0 * (attempt + 1))
        stats['fail'].append(code)


async def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument('--code', action='append', help='只采集一个或多个代码，可逗号分隔')
    ap.add_argument('--limit', type=int, help='限制代码数；适合小样本')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--allow-full', action='store_true', help='明确允许无代码筛选的全量采集')
    args = ap.parse_args(argv)
    codes = sorted({x.strip() for value in (args.code or []) for x in value.split(',') if x.strip()})
    if not codes and not args.allow_full:
        raise SystemExit('拒绝默认全量采集：请使用 --code，或明确传 --allow-full')
    if not codes:
        codes = universe_codes()
    if args.limit is not None:
        codes = codes[:max(args.limit, 0)]
    stats = {'requested': len(codes), 'ok': 0, 'events': 0, 'empty_table': 0,
             'invalid_page': 0, 'bad_status': 0, 'requests': 0, 'fail': [],
             'dry_run': args.dry_run}
    sem = asyncio.Semaphore(1 if args.dry_run else 4)
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=12, trust_env=False, headers=HEADERS,
                                 follow_redirects=True) as client:
        await asyncio.gather(*(one(client, sem, code, stats, dry_run=args.dry_run) for code in codes))
    stats['elapsed_s'] = round(time.monotonic() - t0, 3)
    print('DRY-RUN' if args.dry_run else 'SAVED', stats)
    return stats


if __name__ == '__main__':
    asyncio.run(main())
