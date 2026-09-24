#!/usr/bin/env python3
"""只补本地BJ日线缺失成交额；按代码保存失败次数，不尝试旧无价证券。"""
import argparse, asyncio, json, sqlite3, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx
from app.core.store import store
from backfill_stock_kline_tx import ths_amounts
from backfill_bj_kline import HEADERS, SOURCE

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--max-minutes', type=float, default=10)
    args = parser.parse_args()
    deadline = time.monotonic() + min(120, max(0, args.max_minutes)) * 60
    fixed, skipped, failures = 0, [], []
    with store._conn() as conn:
        gaps = conn.execute("SELECT stock_code, trade_date FROM stock_kline_daily WHERE source=? AND amount IS NULL ORDER BY stock_code, trade_date", (SOURCE,)).fetchall()
    codes = {}
    for row in gaps:
        codes.setdefault(row['stock_code'], []).append(row['trade_date'])
    async with httpx.AsyncClient(trust_env=False, headers=HEADERS, timeout=20) as client:
        for code, dates in codes.items():
            if time.monotonic() >= deadline:
                break
            key = 'backfill:bj_amount:failures:' + code
            failure = store.kv_get(key, {}) or {}
            if failure.get('attempts', 0) >= 3:
                skipped.append(code)
                continue
            amounts = await ths_amounts(client, code, sorted({d[:4] for d in dates}))
            pairs = [(amounts[d], d, code) for d in dates if amounts.get(d) is not None]
            with store._conn() as conn:
                before = conn.total_changes
                conn.executemany("UPDATE stock_kline_daily SET amount=? WHERE trade_date=? AND stock_code=? AND amount IS NULL", pairs)
                changed = conn.total_changes - before
            fixed += changed
            left = [d for d in dates if amounts.get(d) is None]
            if left:
                rec = {'attempts':failure.get('attempts',0)+1, 'missing_dates':left, 'reason':'THS年度字段缺失或请求失败'}
                store.kv_set(key, rec)
                failures.append({'code':code, **rec})
            print({'code':code, 'actual_updated':changed, 'missing':len(left)}, flush=True)
            await asyncio.sleep(1)
    report = {'actual_updated':fixed,'skipped_at_limit':skipped,'failures':failures,'stopped_at_deadline':time.monotonic()>=deadline}
    Path('logs/bj_amount_repair_v1_1.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(report)
if __name__ == '__main__':
    asyncio.run(main())
