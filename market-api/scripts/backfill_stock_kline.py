#!/usr/bin/env python3
"""回填 stock_kline_daily：fqt=0 日K（官方昨收/除权检测）+ 除权股 hfq 总收益因子。

代码清单取最新 ml_r1:universe 记录（含停牌股），无 universe 时拒绝运行。
用法: .venv/bin/python scripts/backfill_stock_kline.py --shard 0 --shards 2
"""
import argparse
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.store import store
from app.datasources import eastmoney
from app.services import ml_r1_kline


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--shard', type=int, default=0)
    ap.add_argument('--shards', type=int, default=1)
    ap.add_argument('--beg', default='20251001')
    ap.add_argument('--end', default='20260914')
    args = ap.parse_args()
    codes: list[str] = []
    for k in reversed(store.kv_get('ml_r1:universe:index', []) or []):
        rec = store.kv_get(k)
        if rec and rec.get('stocks'):
            codes = [s['code'] for s in rec['stocks']]
            break
    if not codes:
        raise SystemExit('no universe record; run collect_ml_universe.py first')
    shard = [c for i, c in enumerate(sorted(set(codes))) if i % args.shards == args.shard]
    print(f'shard {args.shard}/{args.shards}: {len(shard)} codes beg={args.beg} end={args.end}', flush=True)
    done = fail = act = rows = 0
    for i, code in enumerate(shard):
        try:
            bars = await eastmoney.kline_history(code, 0, fqt=0, beg=args.beg, end=args.end)
            if not bars:
                done += 1
                continue
            rowset = ml_r1_kline.bars_to_rows(code, bars)
            if any(r['action_day'] for r in rowset):
                bars2 = await eastmoney.kline_history(code, 0, fqt=2, beg=args.beg, end=args.end)
                hfq = {b['date']: b['close'] for b in bars2 if b.get('close')}
                rowset = ml_r1_kline.bars_to_rows(code, bars, hfq)
                act += 1
            store.kline_daily_save(rowset)
            rows += len(rowset)
            done += 1
        except Exception as exc:  # noqa: BLE001
            fail += 1
            print(f'FAIL {code}: {type(exc).__name__}', flush=True)
        if (i + 1) % 100 == 0:
            print(f'progress {i+1}/{len(shard)} rows={rows} action_stocks={act} fail={fail}', flush=True)
    print(f'DONE shard={args.shard} ok={done} fail={fail} rows={rows} action_stocks={act}', flush=True)

if __name__ == '__main__':
    asyncio.run(main())
