#!/usr/bin/env python3
"""腾讯WAF限流后的K线慢速补采（免费源，幂等可重跑）。

2026-09-15 首轮两分片回填触发腾讯WAF（HTTP 501 → waf.tencent.com 拦截页），
universe 5912 只中仅 831 只有K线（tx_fail≈4973 只 JSONDecodeError）。

策略：
1) 按证券、交易日、OHLC及成交额检查缺口（不按总行数判断完整）；
   北交所(43/83/87/92开头)腾讯无数据，直接跳过并记录（见问题清单）；
   已知空返回（退市/长期停牌）记入 kv backfill:tx:empty，不再反复重试。
2) 探测WAF：未恢复则每 --probe-interval 秒重探，最多累计3次失败，最长2小时后退出。
3) 恢复后 concurrency=1、每只间隔 0.8~1.6s 随机；再次命中WAF退避 --backoff 秒重探。
4) 进度 kv backfill:tx:retry_done（与首轮 checkpoint 独立）。

用法: .venv/bin/python scripts/retry_kline_tx.py [--min-rows 200] [--max-hours 10]
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

from app.core.store import store
import backfill_stock_kline_tx as bf


def missing_codes(min_rows=0, start='2025-12-01', end='2026-09-14'):
    """按证券×已发布交易日×OHLC/成交额检查；min_rows仅保留命令行兼容。"""
    from app.services.ml_r1_service import calendar_days
    calendar = [d for d in calendar_days() if start <= d <= end]
    if not calendar:
        raise ValueError('Missing verified trading calendar')
    index = store.kv_get('ml_r1:universe:index', []) or []
    records = [store.kv_get(k) for k in index]
    latest = max((r for r in records if r), key=lambda r:r.get('collected_at',''), default={})
    stocks = {s['code']:str(s.get('list_date') or '') for s in latest.get('stocks',[])}
    with store._conn() as conn:
        rows = conn.execute('SELECT stock_code,trade_date FROM stock_kline_daily WHERE trade_date BETWEEN ? AND ? '
                            'AND open>0 AND high>0 AND low>0 AND close>0 AND amount>=0', (start,end)).fetchall()
    have = {}
    for r in rows:have.setdefault(r['stock_code'],set()).add(r['trade_date'])
    todo,bj=[],[]
    for code,listed in stocks.items():
        if code[:2] in ('43','83','87','92'):
            bj.append(code);continue
        if len(listed)!=8 or not listed.isdigit():continue
        listed=listed[:4]+'-'+listed[4:6]+'-'+listed[6:]
        expected={d for d in calendar if d>=listed}
        if expected-have.get(code,set()):todo.append(code)
    return todo,bj


FAILURE_KEY = 'backfill:tx:source_failures'

def record_failure():
    n = int(store.kv_get(FAILURE_KEY, 0)) + 1
    store.kv_set(FAILURE_KEY, n)
    return n >= 3


async def probe(c: httpx.AsyncClient) -> bool:
    try:
        d = await bf.tx_kline(c, 'sh600519', '', '2026-09-01', '2026-09-14')
        return bool(d)
    except Exception:  # noqa: BLE001 - WAF页/网络错都算未恢复
        return False


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--beg', default='2025-09-01')
    ap.add_argument('--keep-from', default='2025-10-01')
    ap.add_argument('--end', default='2026-09-14')
    ap.add_argument('--min-rows', type=int, default=200)
    ap.add_argument('--probe-interval', type=int, default=600)
    ap.add_argument('--backoff', type=int, default=900)
    ap.add_argument('--max-hours', type=float, default=2.0)
    args = ap.parse_args()

    if int(store.kv_get(FAILURE_KEY, 0)) >= 3:
        print('source attempt limit reached; no request sent', flush=True)
        return
    todo, bj = missing_codes(args.min_rows, args.keep_from, args.end)
    done_key = 'backfill:tx:retry_done'
    done = set(store.kv_get(done_key, []) or [])
    empty = set(store.kv_get('backfill:tx:empty', []) or [])
    todo = [c for c in todo if c not in empty]
    years = sorted({args.beg[:4], args.end[:4]})
    print(f'missing={len(todo)} bj_skip={len(bj)} done={len(done)} empty_skip={len(empty)}', flush=True)
    if not todo:
        return

    t0 = time.monotonic()
    deadline = t0 + min(2, max(0, args.max_hours)) * 3600
    sem = asyncio.Semaphore(1)
    stats: dict = {'rows': 0, 'action_days': 0, 'amt_missing': 0,
                   'tx_fail': [], 'tx_empty': [], 'ths_fail': [], 'bad_code': []}
    async with httpx.AsyncClient(timeout=15, trust_env=False, headers=bf.HEADERS,
                                 follow_redirects=True) as c:
        while not await probe(c):
            if record_failure() or time.monotonic() > deadline:
                print('WAF not recovered before deadline; exit honestly', flush=True)
                return
            print(f'WAF active; sleep {args.probe_interval}s', flush=True)
            await asyncio.sleep(args.probe_interval)
        print('WAF recovered; start slow retry', flush=True)
        i = 0
        while i < len(todo):
            if time.monotonic() > deadline:
                print(f'deadline reached at {i}/{len(todo)}; exit', flush=True)
                break
            code = todo[i]
            rows = await bf.one_code(c, sem, code, args.beg, args.end,
                                     args.keep_from, years, stats)
            if rows:
                store.kline_daily_save(rows)
                done.add(code)
            if code in stats['tx_empty']:
                empty.add(code)
                stats['tx_empty'].remove(code)
            if stats['tx_fail'] and stats['tx_fail'][-1][0] == code:
                # 再次命中WAF：退避后重探，不计入done
                stats['tx_fail'].pop()
                if record_failure():
                    print("source attempt limit reached", flush=True)
                    break
                print(f'WAF re-triggered at {code} ({i}/{len(todo)}); backoff {args.backoff}s', flush=True)
                await asyncio.sleep(args.backoff)
                while not await probe(c):
                    if record_failure() or time.monotonic() > deadline:
                        print('deadline during backoff; exit', flush=True)
                        store.kv_set(done_key, sorted(done))
                        store.kv_set('backfill:tx:empty', sorted(empty))
                        return
                    await asyncio.sleep(args.probe_interval)
                continue  # 重试同一只
            i += 1
            if i % 50 == 0:
                store.kv_set(done_key, sorted(done))
                store.kv_set('backfill:tx:empty', sorted(empty))
                print(f'[{i}/{len(todo)}] rows={stats["rows"]} empty={len(empty)} '
                      f'{int(time.monotonic() - t0)}s', flush=True)
            await asyncio.sleep(random.uniform(0.8, 1.6))
    store.kv_set(done_key, sorted(done))
    store.kv_set('backfill:tx:empty', sorted(empty))
    print(f'FINAL retry done={len(done)} empty={len(empty)} rows={stats["rows"]} '
          f'amt_missing={stats["amt_missing"]} ths_fail={len(stats["ths_fail"])} '
          f'elapsed={int(time.monotonic() - t0)}s', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
