# ponytail: one-shot coverage probe; delete when done
"""Probe stocks/pct coverage: members vs daily_close by exchange."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import plate_flow  # noqa: E402

store = plate_flow.store


def exch(code: str) -> str:
    c = str(code)
    if c.startswith(("5", "6", "9")) and not c.startswith("920"):
        # 6xxxxx SH main/STAR; 5xxxxx funds sometimes; 9 rarely
        if c.startswith("6"):
            return "SH"
        if c.startswith("5"):
            return "SH_fund"
    if c.startswith(("0", "3")):
        return "SZ"
    if c.startswith(("4", "8")) or c.startswith("920"):
        return "BJ"
    if c.startswith("688") or c.startswith("689"):
        return "SH"  # STAR already under 6
    return "OTHER"


def main() -> None:
    plate = sys.argv[1] if len(sys.argv) > 1 else "801001"
    day = sys.argv[2] if len(sys.argv) > 2 else "2026-09-22"
    days = int(sys.argv[3]) if len(sys.argv) > 3 else 10

    members = plate_flow.plate_members(plate, day)
    full = plate_flow.plate_members_full(plate, day)
    print(f"plate={plate} day={day} window={days}")
    print(f"members={len(members)} full={len(full)} extra_full={len(full - members)}")

    by_ex = {}
    for c in members:
        by_ex.setdefault(exch(c), set()).add(c)
    print("members_by_ex:", {k: len(v) for k, v in sorted(by_ex.items())})

    data = plate_flow.stocks_pct(plate, day, days)
    stocks = data.get("stocks") or {}
    hit = []
    for iv, arr in stocks.items():
        for s in arr:
            hit.append(s["stock_code"])
    hit_set = set(hit)
    print(f"pct_ge20={len(hit_set)} buckets={{k:len(v) for k,v in stocks.items()}}".replace(
        "{k:len(v) for k,v in stocks.items()}", str({k: len(v) for k, v in stocks.items()})
    ))

    rows = store.daily_close_range(day, max(days, 1))
    by_date = {}
    for r in rows:
        by_date.setdefault(r["trade_date"], {})[str(r["stock_code"])] = r
    dates_have = sorted(by_date)
    print(f"close_dates_in_window={len(dates_have)} first={dates_have[0] if dates_have else None} last={dates_have[-1] if dates_have else None}")
    if day not in by_date or len(dates_have) < days + 1:
        print("FAIL: insufficient trade calendar for window")
        return
    base_date = dates_have[dates_have.index(day) - days]
    end_map, base_map = by_date[day], by_date.get(base_date) or {}
    print(f"base_date={base_date} end_count={len(end_map)} base_count={len(base_map)}")

    miss_end = []
    miss_base = []
    both_ok = []
    below20 = []
    for code in sorted(members):
        e, b = end_map.get(code), base_map.get(code)
        if not e or not e.get("close"):
            miss_end.append(code)
            continue
        if not b or not b.get("close"):
            miss_base.append(code)
            continue
        pct = (float(e["close"]) / float(b["close"]) - 1) * 100
        both_ok.append(code)
        if pct < 20:
            below20.append(code)

    print(f"both_ok={len(both_ok)} miss_end={len(miss_end)} miss_base={len(miss_base)} below20={len(below20)}")

    def bucket_ex(codes):
        c = {}
        for x in codes:
            c[exch(x)] = c.get(exch(x), 0) + 1
        return c

    print("miss_end_by_ex:", bucket_ex(miss_end))
    print("miss_base_by_ex:", bucket_ex(miss_base))
    print("hit_ge20_by_ex:", bucket_ex(hit_set))
    print("both_ok_by_ex:", bucket_ex(both_ok))

    # sample miss
    print("miss_end_sample:", miss_end[:15])
    print("miss_base_sample:", miss_base[:15])
    # SH share in members vs hit
    sh_m = len(by_ex.get("SH", ()))
    sh_h = sum(1 for c in hit_set if exch(c) == "SH")
    print(f"SH members={sh_m} SH in ge20={sh_h}")


if __name__ == "__main__":
    main()
