"""Ensure THS limit pools cover every plate_rank_daily date (ladder input for all themes)."""
from __future__ import annotations

import asyncio
import sys

from app.core.store import store
from app.services import pools


async def ensure_day(day: str) -> dict:
    out = {"date": day, "ths_up": None, "ths_broken": None, "errors": []}
    if pools.published("ths_up", day) is None:
        try:
            r = await pools.collect_ths(day)
            out["ths_up"] = r["total"]
        except Exception as e:
            out["errors"].append(f"ths_up:{type(e).__name__}:{e}")
    else:
        out["ths_up"] = "exists"
    if pools.published("ths_broken", day) is None:
        try:
            r = await pools.collect_ths_broken(day)
            out["ths_broken"] = r["total"]
        except Exception as e:
            out["errors"].append(f"ths_broken:{type(e).__name__}:{e}")
    else:
        out["ths_broken"] = "exists"
    return out


async def main(limit: int = 40) -> None:
    days = store.plate_rank_dates(17, limit)
    print("rank_days", days)
    for d in days:
        r = await ensure_day(d)
        print(r)
        await asyncio.sleep(0.3)


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    asyncio.run(main(n))
