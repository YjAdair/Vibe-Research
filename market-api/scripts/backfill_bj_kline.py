#!/usr/bin/env python3
"""北交所 raw OHLCV（新浪）及真实成交额（同花顺）补缺。
缺行不等于停牌，前bar收盘只作原始比较信息，不认证为官方昨收。
默认含全年前至少20交易日；按代码持久保存失败次数，截止后保存断点。
不推断无价证券上市状态，不承诺某日必然可发布评分。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

from backfill_stock_kline_tx import ths_amounts
from app.core.store import store

BACKEND = Path(__file__).resolve().parents[1]
DB = BACKEND / 'zzquant.db'
SINA_URL = 'https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData'
SOURCE = 'sina_ths_bj_v1'
HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}


def bj_codes(conn: sqlite3.Connection) -> list[str]:
    row = conn.execute(
        "SELECT value FROM kv WHERE key LIKE 'ml_r1:universe:2%' ORDER BY key DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise SystemExit('no universe snapshot in kv')
    univ = json.loads(row['value'])
    return [str(s['code']) for s in univ['stocks']
            if str(s['code'])[:2] in ('43', '83', '87', '92')]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default='2025-11-01')
    ap.add_argument('--code', action='append')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--sleep', type=float, default=0.35)
    ap.add_argument('--max-minutes', type=float, default=20)
    args = ap.parse_args()
    today = date.today().isoformat()
    years = sorted({args.since[:4], today[:4]})

    conn = sqlite3.connect(DB, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=60000')
    codes = bj_codes(conn)
    codes = [code for code in codes if conn.execute(
        'SELECT 1 FROM stock_kline_daily WHERE stock_code=? AND trade_date>=? LIMIT 1',
        (code, '2026-09-01')).fetchone()]
    if args.code:
        codes = [c for c in codes if c in args.code]
    if args.limit:
        codes = codes[: args.limit]
    t0 = time.monotonic()
    source_failures = 0
    deadline = t0 + min(120, max(1, args.max_minutes)) * 60
    print(f'BJ codes: {len(codes)} since={args.since} years={years} today={today}', flush=True)

    inserted = 0
    amt_missing = 0
    empty: list[str] = []
    failed: list[tuple[str, str]] = []
    async with httpx.AsyncClient(trust_env=False, headers=HEADERS, timeout=20) as c:
        for i, code in enumerate(codes, 1):
            if time.monotonic() >= deadline or source_failures >= 3:
                print('Stopped at time/three-failure limit', flush=True)
                break
            failure_key = 'backfill:bj_ohlc:failures:' + code
            failure_record = store.kv_get(failure_key, {}) or {}
            if failure_record.get('attempts', 0) >= 3:
                continue
            try:
                r = await c.get(SINA_URL, params={'symbol': f'bj{code}', 'scale': 240,
                                                  'ma': 'no', 'datalen': 300})
                r.raise_for_status()
                bars = r.json()
                if not isinstance(bars, list):
                    raise ValueError(f'bad sina payload: {r.text[:60]}')
                amounts = await ths_amounts(c, code, years)
                rows = []
                for j, b in enumerate(bars):
                    day = b['day']
                    if day < args.since or day >= today:
                        continue
                    prev = float(bars[j - 1]['close']) if j > 0 else None
                    amt = amounts.get(day)
                    if amt is None:
                        amt_missing += 1
                    rows.append((day, code, float(b['open']), float(b['high']),
                                 float(b['low']), float(b['close']), None,
                                 float(b['volume']) / 100.0, amt, json.dumps({'raw_prev_close':prev,'return_basis':None,'state_verified':False,'action_reason':'missing_official_reference'})))
                if rows:
                    now_s = time.strftime('%Y-%m-%dT%H:%M:%S')
                    before_changes = conn.total_changes
                    conn.executemany(
                        'INSERT OR IGNORE INTO stock_kline_daily (trade_date, stock_code,'
                        ' open, high, low, close, prev_close, volume, amount, tr_factor,'
                        ' action_day, close_hfq, source, updated_at, input_meta)'
                        ' VALUES (?,?,?,?,?,?,?,?,?,NULL,0,NULL,?,?,?)',
                        [(*row[:9], SOURCE, now_s, row[9]) for row in rows])
                    conn.commit()
                    inserted += conn.total_changes - before_changes
                else:
                    empty.append(code)
                if i % 25 == 0 or i == len(codes):
                    print(f'[{i}/{len(codes)}] inserted={inserted} empty={len(empty)} '
                          f'failed={len(failed)} amt_missing={amt_missing} '
                          f'{time.monotonic() - t0:.0f}s', flush=True)
            except Exception as exc:  # noqa: BLE001
                source_failures += 1
                store.kv_set(failure_key, {'attempts':failure_record.get('attempts',0)+1,'error':type(exc).__name__,'since':args.since})
                failed.append((code, type(exc).__name__))
                print(f'[{i}/{len(codes)}] {code} FAILED: {exc}', flush=True)
            await asyncio.sleep(args.sleep)
    print(f'done: inserted={inserted} empty_codes={sorted(empty)} failed={len(failed)} '
          f'amt_missing_days={amt_missing}', flush=True)
    for code, err in failed:
        print(f'  {code}: {err}')
    Path('logs/bj_backfill_v1_1_latest.json').write_text(json.dumps({'inserted':inserted,'failed':failed,'amount_missing_days':amt_missing,'since':args.since,'codes':codes,'stopped_at_deadline':time.monotonic()>=deadline},ensure_ascii=False,indent=2))
    conn.close()
    if failed:
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
