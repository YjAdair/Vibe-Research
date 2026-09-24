"""补齐近端交易日异动快照（收盘派生，供涨幅提醒 join）。

用法：python tools/backfill_movement_recent.py [n_days]
"""
from __future__ import annotations
import asyncio
import sys

from app.core.store import store
from app.services import movement


async def main(n: int = 15) -> None:
    days = store.daily_close_dates(n)
    ok, skip, err = [], [], []
    for day in reversed(days):
        if store.movement_get(day):
            skip.append(day)
            continue
        try:
            snap = await movement.collect(day)
            ok.append(f"{day}:{snap.get('total')}")
            print("ok", day, snap.get("total"), flush=True)
        except Exception as exc:  # noqa: BLE001
            err.append(f"{day}:{type(exc).__name__}:{exc}")
            print("fail", day, type(exc).__name__, exc, flush=True)
    print({"ok": ok, "skip": skip, "err": err})


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    asyncio.run(main(n))
