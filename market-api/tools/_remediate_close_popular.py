"""Retry daily close + popular align check."""
import asyncio
import json
import sqlite3
import urllib.request

from app.services import daily_close, popular

DAY = "2026-09-24"
BOARD = "801159"
HDR = {"User-Agent": "Mozilla/5.0", "Referer": "https://quant.zizizaizai.com/"}


async def try_close():
    for i in range(3):
        try:
            n = await daily_close.backfill_day(DAY)
            print("daily_close ok", n)
            return n
        except Exception as e:
            print(f"daily_close try{i+1}", type(e).__name__, e)
            await asyncio.sleep(3)
    return 0


def get(url: str):
    req = urllib.request.Request(url, headers=HDR)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def local(path: str):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=90) as r:
        return json.loads(r.read().decode())


async def main():
    await try_close()
    con = sqlite3.connect("zzquant.db")
    print("close rows", con.execute("select count(*) from daily_close where trade_date=?", (DAY,)).fetchone()[0])
    snap = popular.published(DAY)
    print("popular published", None if not snap else {k: snap.get(k) for k in ("date", "source", "total")})

    o = get(f"https://api.zizizaizai.com/v3/market/plates/17/{BOARD}/stocks/rank?with_pct=1&limit=15&date1={DAY}&page=1")
    l = local(f"/v3/market/plates/17/{BOARD}/stocks/rank/list?date1={DAY}&page=1&limit=15&with_pct=1")
    od = (o.get("data") or {}).get("list") or []
    ld = (l.get("data") or {}).get("list") or []
    print("origin popular top", [(r.get("stock_code"), r.get("rank"), r.get("px_change_rate")) for r in od[:10]])
    print("local  popular top", [(r.get("stock_code"), r.get("rank"), r.get("px_change_rate")) for r in ld[:10]])
    print("local meta", (l.get("data") or {}).get("meta"))
    oc = [r.get("stock_code") for r in od[:10]]
    lc = [r.get("stock_code") for r in ld[:10]]
    print("equal", oc == lc, "jaccard", len(set(oc) & set(lc)) / max(1, len(set(oc) | set(lc))))

    # pct today
    lp = local(f"/v3/market/plates/17/{BOARD}/stocks/pct?date1={DAY}&days=10")
    stocks = (lp.get("data") or {}).get("stocks") or {}
    print("pct today counts", {k: len(v) for k, v in stocks.items() if isinstance(v, list)})
    print("pct meta", (lp.get("data") or {}).get("meta"))


if __name__ == "__main__":
    asyncio.run(main())
