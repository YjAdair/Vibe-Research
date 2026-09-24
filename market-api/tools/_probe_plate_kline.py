from app.config import settings
from app.core.store import store
import asyncio
from app.services import market

print("enable_origin_reference", settings.enable_origin_reference)
with store._conn() as conn:
    rows = conn.execute("select kind, plate_code, last_date from plate_kline_origin").fetchall()
    print("kline_origin", [(r[0], r[1], r[2]) for r in rows])
    print("daily_close", tuple(conn.execute(
        "select min(trade_date), max(trade_date), count(distinct trade_date) from daily_close"
    ).fetchone()))
    print("members", tuple(conn.execute(
        "select min(trade_date), max(trade_date), count(*) from plate_members_snapshots"
    ).fetchone()))

async def main():
    for code in ("803014", "801001", "801120"):
        data = await market.plate_kline_contract(code, "main", 5)
        print(code, "x", len(data.get("x") or []), "status", data.get("status"),
              "reason", data.get("reason"), "kind", data.get("series_kind"),
              "sample", (data.get("x") or [])[-2:], (data.get("y") or [])[-1:])

asyncio.run(main())
