"""从原站 /v3/open/sentiment/media/ths/top 回填人气深表（用户授权过渡数据源）。

原站按日期返回当日全市场人气存档（~5500），历史可回放。写入 popular_snapshots，
source=zizizaizai_ths_top_calibration，经 popular.published 产品可读。
用法: python scripts/backfill_popular_ths_top.py [start_date] [end_date] [--force]
不传参数：回填 plate_rank_daily 覆盖日 + 今天（缺深表才拉）。
--force: 强制重拉。
需 ZZQUANT_ENABLE_ORIGIN_REFERENCE=1（或本脚本临时开启）。
"""
import asyncio
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('ZZQUANT_ENABLE_ORIGIN_REFERENCE', '1')

from app.core.store import store  # noqa: E402
from app.services.popular import DEEP_MIN_TOTAL  # noqa: E402

ORIG = 'https://api.zizizaizai.com'
THROTTLE = 6.6  # 原站限流 10 req/min
SOURCE = 'zizizaizai_ths_top_calibration'


async def fetch_day(client, day: str) -> list | None:
    resp = await client.get(
        ORIG + '/v3/open/sentiment/media/ths/top',
        params={'date1': day, 'top_n': 20000},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError('HTTP %s for %s' % (resp.status_code, day))
    body = resp.json()
    if body.get('code') != 20000:
        raise RuntimeError('API error %s for %s' % (body.get('code'), day))
    return body.get('data')


def to_payload(day: str, rows: list) -> dict:
    rows = sorted(rows, key=lambda r: (r.get('rank') or 0, r.get('symbol_code') or ''))
    items = []
    for r in rows:
        items.append({
            'symbol_code': r['symbol_code'],
            'symbol_name': '',
            'rank': r.get('rank'),
            'src_rank': r.get('rank'),
            # 缺失≠0：原站给 null 时保留 None
            'rank_diff': r.get('rank_diff'),
            'heat': r.get('attention'),
            'attention': r.get('attention'),
            'market': None,
            'concept_tag': [],
        })
    return {
        'date': day,
        'complete': True,
        'total': len(items),
        'items': items,
        'source': SOURCE,
        'kind': 'day',
        'quote_coverage': 0,
        'history_note': 'origin ths/top daily archive; user-authorized bridge until free deep hist exists',
        'collected_at': datetime.now().isoformat(),
    }


def already_deep(day: str) -> bool:
    hit = store.popular_get(day)
    return bool(hit and hit.get('source') == SOURCE and (hit.get('total') or 0) >= DEEP_MIN_TOTAL)


async def main(start: str | None, end: str | None, force: bool = False):
    import httpx
    import sqlite3
    conn = sqlite3.connect(store.path)
    dates = [r[0] for r in conn.execute(
        'SELECT DISTINCT trade_date FROM plate_rank_daily ORDER BY trade_date DESC').fetchall()]
    conn.close()
    today = datetime.now().date().isoformat()
    if today not in dates:
        dates = [today] + dates
    if start:
        dates = [d for d in dates if d >= start]
    if end:
        dates = [d for d in dates if d <= end]
    # 含今天；缺深表才拉
    todo = [d for d in dates if force or not already_deep(d)]
    print('dates in range: %d, todo: %d (force=%s)' % (len(dates), len(todo), force), flush=True)
    ok = fail = 0
    async with httpx.AsyncClient() as client:
        for i, day in enumerate(todo):
            try:
                rows = await fetch_day(client, day)
            except Exception as exc:
                fail += 1
                print('[%s] FAIL %s' % (day, exc), flush=True)
                if fail >= 5:
                    print('too many failures, aborting', flush=True)
                    break
                time.sleep(THROTTLE)
                continue
            if rows:
                store.popular_save(day, to_payload(day, rows))
                ok += 1
                print('[%s] %d/%d ok rows=%d' % (day, i + 1, len(todo), len(rows)), flush=True)
            else:
                print('[%s] empty (non-trading day upstream?)' % day, flush=True)
            if i % 10 == 9:
                print('checkpoint: ok=%d fail=%d' % (ok, fail), flush=True)
            time.sleep(THROTTLE)
    print('DONE ok=%d fail=%d' % (ok, fail), flush=True)


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if a != '--force']
    asyncio.run(main(args[0] if len(args) > 0 else None,
                     args[1] if len(args) > 1 else None,
                     force='--force' in sys.argv[1:]))
