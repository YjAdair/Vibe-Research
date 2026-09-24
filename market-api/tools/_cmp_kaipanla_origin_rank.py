"""Live kaipanla vs origin rank delta."""
import asyncio
import json
import urllib.request

from app.datasources import kaipanla_plate

DAY = "2026-09-24"


async def main():
    rows = await kaipanla_plate.fetch_realtime_rank()
    top = sorted(rows, key=lambda r: -r["score"])[:12]
    print("kaipanla live top12:")
    for i, r in enumerate(top, 1):
        print(f"{i:2} {r['plate_code']} {r['plate_name']} score={r['score']} rate={r['rate']}")

    req = urllib.request.Request(
        f"https://api.zizizaizai.com/v3/market/plates/17/rank/days?date2={DAY}&n_days=1&data_src=1&n_type=9",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quant.zizizaizai.com/"},
    )
    o = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
    data = o.get("data") or o
    lst = data if isinstance(data, list) else (data.get("list") or [])
    print("\norigin top12:")
    for i, r in enumerate(lst[:12], 1):
        sc = r.get("sum_score", r.get("score"))
        rt = r.get("sum_rate", r.get("rate"))
        print(f"{i:2} {r.get('plate_code')} {r.get('plate_name')} score={sc} rate={rt}")

    om = {str(r.get("plate_code")): r for r in lst}
    print("\norder: kaipanla vs origin")
    for i, r in enumerate(top, 1):
        ocode = str(lst[i - 1].get("plate_code")) if i - 1 < len(lst) else ""
        same = r["plate_code"] == ocode
        od = om.get(r["plate_code"])
        osc = od.get("sum_score", od.get("score")) if od else None
        print(
            f"{i:2} k={r['plate_code']} o_at_rank={ocode} same_pos={same} "
            f"score_k={r['score']} score_o={osc} d={(float(r['score'])-float(osc)) if osc is not None else None}"
        )


if __name__ == "__main__":
    asyncio.run(main())
