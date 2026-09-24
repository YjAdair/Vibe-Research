# probe: 汇通能源 / 市北高新 vs local stocks/pct
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlite3
from app.services import plate_flow

DB = Path(__file__).resolve().parents[1] / "zzquant.db"
con = sqlite3.connect(DB)

NAMES = ("汇通能源", "市北高新")
codes = {}
for name in NAMES:
    rows = con.execute(
        "select stock_code, stock_name, count(*) from daily_close "
        "where stock_name like ? group by stock_code, stock_name order by count(*) desc limit 5",
        (f"%{name}%",),
    ).fetchall()
    print("name_hit", name, rows)
    if rows:
        codes[name] = rows[0][0]

# also try known codes
for guess in ("600605", "600604", "600649", "600639"):
    r = con.execute(
        "select stock_code, stock_name from daily_close where stock_code=? order by trade_date desc limit 1",
        (guess,),
    ).fetchone()
    print("guess", guess, r)

day = "2026-09-22"
for days in (5, 10, 20):
    print("\n=== window", days, "===")
    rows = plate_flow.store.daily_close_range(day, days)
    by_date = {}
    for r in rows:
        by_date.setdefault(r["trade_date"], {})[str(r["stock_code"])] = r
    dates = sorted(by_date)
    if day not in by_date or len(dates) < days + 1:
        print("bad calendar", dates)
        continue
    base = dates[dates.index(day) - days]
    end_m, base_m = by_date[day], by_date[base]
    print("base", base)
    for name, code in list(codes.items()) + [("guess605", "600605"), ("guess604", "600604")]:
        if name.startswith("guess") and name[5:] in {c for c in codes.values()}:
            continue
        e, b = end_m.get(code), base_m.get(code)
        pct = None
        if e and b and e.get("close") and b.get("close"):
            pct = round((float(e["close"]) / float(b["close"]) - 1) * 100, 2)
        print(f"  {name} {code}: end={e and e.get('close')} base={b and b.get('close')} pct={pct} "
              f"name_e={e and e.get('stock_name')} name_b={b and b.get('stock_name')}")

# membership in top plates
members_plates = []
plates = plate_flow.load_members()
for plate, blob in plates.items():
    stocks_by_date = blob.get("stocks") or {}
    day_key = plate_flow._nearest_member_day(stocks_by_date, day)
    if not day_key:
        continue
    subs = stocks_by_date.get(day_key) or {}
    allc = set()
    for arr in subs.values():
        allc.update(str(x) for x in (arr or []))
    for name, code in codes.items():
        if code in allc:
            members_plates.append((plate, name, code, blob.get("name") or plate))

print("\nin_members:", members_plates[:30], "count", len(members_plates))

# local pct for plates that contain them
seen = set()
for plate, name, code, pname in members_plates:
    if plate in seen:
        continue
    seen.add(plate)
    data = plate_flow.stocks_pct(plate, day, 10)
    hit = None
    for iv, arr in (data.get("stocks") or {}).items():
        for s in arr:
            if s["stock_code"] == code:
                hit = (iv, s)
    print(f"pct plate={plate}({pname}) {name} {code} -> {hit} ge20_n="
          f"{sum(len(v) for v in (data.get('stocks') or {}).values())}")
