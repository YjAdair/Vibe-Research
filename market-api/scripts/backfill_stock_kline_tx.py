#!/usr/bin/env python3
"""腾讯(raw+hfq) + 同花顺(成交额) 回填 stock_kline_daily。

背景：东财 push2his K线主机对本机 IP 全面断连（2026-09-15 实测 0/12），
按免费源优先策略切换：腾讯 ifzq 提供 raw+hfq 日K（OHLCV），同花顺 v6 年度
文件提供成交额（01系列价格为前复权，仅取 amount/volume，价格一律用腾讯 raw）。

口径与 ml_r1_kline.bars_to_rows 一致：
- 官方昨收 = close_raw[d] * hfq[d-1]/hfq[d]（总收益因子倒数），change=close-官方昨收；
- 除权判据 |官方昨收(d)-close(d-1)|>0.005 由 bars_to_rows 执行；
- 同花顺 amount 缺失 → None（诚实缺失，不用 volume*price 猜测）。

用法: .venv/bin/python scripts/backfill_stock_kline_tx.py [--shard 0 --shards 2]
      [--beg 2025-09-01] [--keep-from 2025-10-01] [--end 2026-09-14] [--test 3]
断点: kv backfill:tx:done:{shard} 已完成代码清单，重跑自动跳过。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.core.store import store
from app.services import ml_r1_kline

TZ = ZoneInfo('Asia/Shanghai')
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Referer': 'http://stockpage.10jqka.com.cn/',
}
SOURCE = 'tencent_ifzq_ths_v1'


def tx_symbol(code: str) -> str | None:
    if code.startswith(('60', '68', '90')):
        return 'sh' + code
    if code.startswith(('00', '30', '20')):
        return 'sz' + code
    if code.startswith(('43', '83', '87', '92', '8')):
        return 'bj' + code
    return None


def universe_codes() -> list[str]:
    index = store.kv_get('ml_r1:universe:index', []) or []
    for key in sorted(index, reverse=True):
        rec = store.kv_get(key)
        if rec and rec.get('stocks'):
            return sorted({s['code'] for s in rec['stocks']})
    return []


async def tx_kline(c: httpx.AsyncClient, sym: str, fq: str, beg: str, end: str) -> dict[str, dict]:
    r = await c.get('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get',
                    params={'param': f'{sym},day,{beg},{end},340,{fq}'})
    d = ((r.json().get('data') or {}).get(sym) or {})
    rows = (d.get('hfqday') if fq == 'hfq' else (d.get('day') or d.get('qfqday'))) or []
    out: dict[str, dict] = {}
    for row in rows:
        try:
            out[row[0]] = {'open': float(row[1]), 'close': float(row[2]),
                           'high': float(row[3]), 'low': float(row[4]),
                           'volume': float(row[5]) if row[5] not in ('', None) else None}
        except (ValueError, TypeError, IndexError):
            continue
    return out


async def ths_amounts(c: httpx.AsyncClient, code: str, years: list[str]) -> dict[str, float | None]:
    amounts: dict[str, float | None] = {}
    for y in years:
        r = None
        for attempt in range(3):
            try:
                r = await c.get(f'http://d.10jqka.com.cn/v6/line/hs_{code}/01/{y}.js')
                if r.status_code == 200 and 'Bad Gateway' not in r.text[:200]:
                    break
            except httpx.HTTPError:
                r = None
            await asyncio.sleep(0.5 * (attempt + 1) + 0.1 * (hash(code) % 7))
        if r is None or r.status_code != 200:
            continue
        m = re.search(r'\((.*)\)', r.text, re.S)
        if not m:
            continue
        try:
            j = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        for line in (j.get('data') or '').split(';'):
            p = line.split(',')
            if len(p) >= 7 and len(p[0]) == 8 and p[0].isdigit():
                d = f'{p[0][:4]}-{p[0][4:6]}-{p[0][6:]}'
                try:
                    amounts[d] = float(p[6]) if p[6] else None
                except ValueError:
                    amounts[d] = None
    return amounts


async def one_code(c: httpx.AsyncClient, sem: asyncio.Semaphore, code: str,
                   beg: str, end: str, keep_from: str, years: list[str],
                   stats: dict) -> list[dict]:
    """第一遍只落 raw OHLCV+amount；change=None → tr_factor/prev_close 由
    第二遍公司行动脚本（独立事件源）统一回填。腾讯 hfq 序列逐日因子不一致
    （2026-09-15 实测 000001 相邻非除权日因子漂移 0.2%），不得作为总收益证据。"""
    sym = tx_symbol(code)
    if not sym:
        stats['bad_code'].append(code)
        return []
    async with sem:
        try:
            raw = await tx_kline(c, sym, '', beg, end)
        except Exception as exc:  # noqa: BLE001
            stats['tx_fail'].append((code, type(exc).__name__))
            return []
        try:
            amounts = await ths_amounts(c, code, years)
        except Exception as exc:  # noqa: BLE001
            stats['ths_fail'].append((code, type(exc).__name__))
            amounts = {}
    if not raw:
        stats['tx_empty'].append(code)
        return []
    dates = sorted(d for d in raw if d <= end)
    bars: list[dict] = []
    amt_missing = 0
    for d in dates:
        r0 = raw[d]
        amt = amounts.get(d)
        if amt is None:
            amt_missing += 1
        bars.append({'date': d, 'open': r0['open'], 'close': r0['close'],
                     'high': r0['high'], 'low': r0['low'],
                     'volume': r0['volume'], 'amount': amt, 'change': None})
    rows = ml_r1_kline.bars_to_rows(code, bars, None, keep_from=keep_from, source=SOURCE)
    if amt_missing:
        stats['amt_missing'] += amt_missing
    stats['rows'] += len(rows)
    return rows


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--shard', type=int, default=0)
    ap.add_argument('--shards', type=int, default=1)
    ap.add_argument('--beg', default='2025-09-01')
    ap.add_argument('--keep-from', default='2025-10-01')
    ap.add_argument('--end', default='2026-09-14')
    ap.add_argument('--test', type=int, default=0, help='只跑前N只并打印明细')
    ap.add_argument('--concurrency', type=int, default=4)
    args = ap.parse_args()

    codes = universe_codes()
    if not codes:
        raise SystemExit('no universe snapshot')
    shard = [c for i, c in enumerate(codes) if i % args.shards == args.shard]
    done_key = f"backfill:tx:done:{args.shard}of{args.shards}"
    done = set(store.kv_get(done_key, []) or [])
    todo = [c for c in shard if c not in done]
    if args.test:
        todo = todo[:args.test]
    years = sorted({args.beg[:4], args.end[:4]})
    print(f'universe={len(codes)} shard={args.shard}/{args.shards} size={len(shard)} '
          f'done={len(done)} todo={len(todo)} years={years}', flush=True)

    stats: dict = {'rows': 0, 'action_days': 0, 'amt_missing': 0,
                   'tx_fail': [], 'tx_empty': [], 'ths_fail': [], 'bad_code': []}
    sem = asyncio.Semaphore(args.concurrency)
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=15, trust_env=False, headers=HEADERS,
                                 follow_redirects=True) as c:
        batch: list[dict] = []
        newly: list[str] = []
        for i, code in enumerate(todo):
            rows = await one_code(c, sem, code, args.beg, args.end, args.keep_from, years, stats)
            if rows:
                batch.extend(rows)
            newly.append(code)
            if len(batch) >= 8000 or (i + 1) % 200 == 0 or i + 1 == len(todo):
                if batch:
                    store.kline_daily_save(batch)
                    batch = []
                if newly:
                    store.kv_set(done_key, sorted(done | set(newly)))
                    done |= set(newly)
                    newly = []
                el = time.monotonic() - t0
                print(f'[{i + 1}/{len(todo)}] rows={stats["rows"]} actions={stats["action_days"]} '
                      f'amt_missing={stats["amt_missing"]} tx_fail={len(stats["tx_fail"])} '
                      f'ths_fail={len(stats["ths_fail"])} tx_empty={len(stats["tx_empty"])} '
                      f'{el:.0f}s', flush=True)
    print('FINAL', {k: (v[:10] if isinstance(v, list) else v) for k, v in stats.items()},
          f'elapsed={time.monotonic() - t0:.0f}s', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
