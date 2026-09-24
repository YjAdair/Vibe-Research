"""Backfill THS limit-up/broken pools for trading calendar (ladder history).

Calendar from 上证指数日K（不依赖空的 collector_calendar）。
Skips days that already have ths_up+ths_broken. Free 10jqka only.
"""
from __future__ import annotations

import asyncio
import sys

from app.core.store import store
from app.datasources import tencent
from app.services import pools


def _iso(d: str) -> str:
    d = str(d).replace("-", "")
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


async def calendar(n: int = 200, day_start: str = "2026-01-01") -> list[str]:
    data = await tencent.kline_day_by_symbol("sh000001", n)
    days = [_iso(x) for x in (data.get("x") or [])]
    return [d for d in days if d >= day_start]


async def main(day_start: str = "2026-01-01", n_bars: int = 220) -> None:
    cal = await calendar(n_bars, day_start)
    if not cal:
        raise SystemExit(f"empty calendar start={day_start} n={n_bars}")
    print(f"calendar {cal[0]}..{cal[-1]} n={len(cal)}")

    filled_up = filled_br = skip = fail = empty = 0
    failures = []
    for i, day in enumerate(cal):
        need_up = pools.published("ths_up", day) is None
        need_br = pools.published("ths_broken", day) is None
        if not need_up and not need_br:
            skip += 1
            continue
        try:
            parts = [day]
            if need_up:
                r = await pools.collect_ths(day)
                parts.append(f"up={r['total']}")
                filled_up += 1
                if r["total"] == 0:
                    empty += 1
            else:
                parts.append("up=exists")
            if need_br:
                r = await pools.collect_ths_broken(day)
                parts.append(f"br={r['total']}")
                filled_br += 1
            else:
                parts.append("br=exists")
            print(" ".join(parts))
        except Exception as e:
            fail += 1
            failures.append((day, type(e).__name__, str(e)[:100]))
            print(f"{day} ERR {type(e).__name__}: {e}")
        await asyncio.sleep(0.35)
        if (i + 1) % 30 == 0:
            print(f"... {i+1}/{len(cal)}")

    with store._conn() as c:
        n_ths = c.execute(
            "SELECT COUNT(DISTINCT trade_date) FROM limit_pool_snapshots WHERE pool='ths_up'"
        ).fetchone()[0]
        mn, mx = c.execute(
            "SELECT MIN(trade_date), MAX(trade_date) FROM limit_pool_snapshots WHERE pool='ths_up'"
        ).fetchone()
        nonzero = c.execute(
            """SELECT COUNT(*) FROM limit_pool_snapshots WHERE pool='ths_up'
               AND json_extract(payload,'$.total') > 0"""
        ).fetchone()[0]
    rank = store.plate_rank_dates(17, 80)
    miss_rank = [
        d for d in rank
        if pools.published("ths_up", d) is None and pools.published("up", d) is None
    ]
    print(
        f"done filled_up={filled_up} filled_br={filled_br} skip={skip} fail={fail} "
        f"zero_up_days≈{empty} ths_up_days={n_ths} nonzero={nonzero} "
        f"range={mn}..{mx} rank_miss={miss_rank}"
    )
    if failures[:15]:
        print("failures", failures[:15])


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else "2026-01-01"
    bars = int(sys.argv[2]) if len(sys.argv) > 2 else 220
    asyncio.run(main(start, bars))
