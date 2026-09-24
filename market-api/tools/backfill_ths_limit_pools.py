"""Backfill THS limit-up / broken pools for recent plate-rank days (ladder input)."""
from __future__ import annotations

import asyncio
import sys

from app.core.store import store
from app.services import pools


async def main(days: int = 20) -> None:
    with store._conn() as c:
        rows = c.execute(
            "SELECT DISTINCT trade_date FROM plate_rank_daily WHERE plate_type=17 "
            "ORDER BY trade_date DESC LIMIT ?",
            (days,),
        ).fetchall()
    dates = [r[0] for r in rows]
    print("days", dates)
    for d in dates:
        try:
            up = await pools.collect_ths(d)
            br = await pools.collect_ths_broken(d)
            print(d, "ths_up", up["total"], "ths_broken", br["total"])
        except Exception as e:
            print(d, "ERR", type(e).__name__, e)
        await asyncio.sleep(0.35)


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    asyncio.run(main(n))
