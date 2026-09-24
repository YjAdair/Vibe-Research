#!/usr/bin/env python3
"""ML-R1 盘后链路执行器（collector 接线前的手动/守护方案）。

顺序（点时约束见 PRD）：
1) 等待至 --at（默认15:08，快照 source_as_of>=15:00 门禁）；
2) update_eod(day)：全市场快照 → 当日K线行（除权日用官方昨收因子）+ eod_quote_run 时点 + 新universe快照；
3) build_local_input + precompute 两种 plate_type（当日点时门禁不过的项保持诚实缺口）；
4) collect_all_members：全部板块成员点时采集（为下一交易日09:30门禁准备）。

用法: .venv/bin/python scripts/run_ml_r1_eod.py [--date 2026-09-15] [--at 15:08] [--skip-wait]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import ml_r1_kline, ml_r1_pipeline

TZ = ZoneInfo('Asia/Shanghai')


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=datetime.now(TZ).date().isoformat())
    ap.add_argument('--at', default='15:08')
    ap.add_argument('--skip-wait', action='store_true')
    args = ap.parse_args()

    if not args.skip_wait:
        hh, mm = (int(x) for x in args.at.split(':'))
        while True:
            now = datetime.now(TZ)
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now >= target:
                break
            wait = min((target - now).total_seconds(), 300)
            print(f'waiting for {args.at}; sleep {int(wait)}s', flush=True)
            await asyncio.sleep(wait)

    day = args.date
    print(f'=== EOD run {day} start {datetime.now(TZ).isoformat()}', flush=True)
    eod = await ml_r1_kline.update_eod(day)
    print('update_eod:', json.dumps({k: v for k, v in eod.items() if k != 'universe'},
                                     ensure_ascii=False), flush=True)
    print('universe:', json.dumps(eod.get('universe'), ensure_ascii=False), flush=True)

    for plate_type in (14, 15):
        try:
            payload = ml_r1_pipeline.build_local_input(day, plate_type)
            res = ml_r1_pipeline.precompute(payload, plate_type)
            print(f'precompute pt={plate_type}: status={res.get("status")} '
                  f'topics={res.get("coverage", {}).get("topic_total")} '
                  f'qualified={res.get("coverage", {}).get("qualified_topics")} '
                  f'reasons={res.get("reasons")}', flush=True)
        except Exception as exc:  # noqa: BLE001 - 单侧失败不阻塞另一侧与成员采集
            print(f'precompute pt={plate_type} FAILED: {type(exc).__name__}: {exc}', flush=True)

    from app.core.store import store
    codes = sorted({c for pt in ('14', '15')
                    for c in ml_r1_kline.board_codes_for(day).get(pt, [])})
    if not codes:
        # 当日目录快照可能尚未落库（board_*任务时点差异）：退回最新已知目录
        for pt in (14, 15):
            codes.extend(p['plate_code'] for p in ml_r1_pipeline.native_catalog(pt))
        codes = sorted(set(codes))
    mem = await ml_r1_kline.collect_all_members(codes, concurrency=6)
    print('members:', json.dumps({k: v for k, v in mem.items() if k != 'errors'},
                                  ensure_ascii=False), flush=True)
    print(f'=== EOD run {day} done {datetime.now(TZ).isoformat()} '
          f'elapsed={int(time.monotonic())}s', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
