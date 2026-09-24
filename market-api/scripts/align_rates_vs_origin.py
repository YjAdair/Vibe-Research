"""对拍本地 vs 目标站 stocks/rates（只读校准，不入库）。"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

os.environ["ZZQUANT_ENABLE_ORIGIN_REFERENCE"] = "1"

import httpx

from app.services import plate_flow

ORIG = "https://api.zizizaizai.com"
OUT = Path(__file__).resolve().parents[1] / "data" / "rates_align_report.json"


async def origin_rates(client: httpx.AsyncClient, code: str, day: str, limit: int = 30) -> tuple[list, int | None]:
    rows: list = []
    page = 1
    total = None
    while True:
        r = await client.get(
            f"{ORIG}/v3/market/plates/17/{code}/stocks/rates",
            params={"date1": day, "page": page, "limit": limit},
            timeout=40,
        )
        body = r.json()
        if body.get("code") != 200:
            return [], None
        data = body.get("data") or {}
        chunk = data.get("list") or []
        rows.extend(chunk)
        total = data.get("total")
        if not chunk or (total is not None and len(rows) >= min(total, limit)):
            break
        page += 1
        if page > 2:
            break
        await asyncio.sleep(0.25)
    return rows[:limit], total


async def main() -> None:
    day = "2026-09-21"
    plates = plate_flow.load_members()
    # 优先：有二级树的活跃题材
    codes = []
    for code, v in plates.items():
        sons = v.get("sub_plates") or []
        if len(sons) >= 1 and not (len(sons) == 1 and sons[0].get("code") == code):
            codes.append(code)
        elif code in ("801001", "803023", "801660"):
            codes.append(code)
    # 去重保序，限制本轮对拍规模
    seen = set()
    ordered = []
    for c in ["801001", "803023", "801660"] + codes:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    ordered = ordered[:40]

    report = {"day": day, "plates": [], "summary": {}}
    ok_top10 = 0
    near_total = 0

    async with httpx.AsyncClient(timeout=40) as client:
        for i, code in enumerate(ordered):
            name = (plates.get(code) or {}).get("plate_name") or code
            o_rows, o_total = await origin_rates(client, code, day, 15)
            loc = await plate_flow.plate_stocks_rates_resolved(code, day, 1, 15, False)
            o_codes = [r.get("stock_code") for r in o_rows]
            l_codes = [r["stock_code"] for r in loc["list"]]
            n = min(10, len(o_codes), len(l_codes))
            top10_exact = o_codes[:n] == l_codes[:n] if n else False
            top10_common = len(set(o_codes[:10]) & set(l_codes[:10]))
            # 涨幅逐值（共同代码）
            o_map = {r.get("stock_code"): r.get("px_change_rate") for r in o_rows}
            rate_err = []
            for r in loc["list"][:15]:
                c = r["stock_code"]
                if c in o_map and o_map[c] is not None and r.get("px_change_rate") is not None:
                    diff = abs(float(r["px_change_rate"]) - float(o_map[c]))
                    if diff > 0.02:
                        rate_err.append({"code": c, "local": r["px_change_rate"], "orig": o_map[c], "diff": round(diff, 4)})
            total_ok = o_total is not None and abs((loc["total"] or 0) - o_total) <= 5
            if top10_exact or top10_common >= 9:
                ok_top10 += 1
            if total_ok:
                near_total += 1
            entry = {
                "plate_code": code,
                "plate_name": name,
                "origin_total": o_total,
                "local_total": loc["total"],
                "top10_exact": top10_exact,
                "top10_common": top10_common,
                "origin_top5": [(r.get("stock_code"), r.get("stock_name"), r.get("px_change_rate")) for r in o_rows[:5]],
                "local_top5": [(r["stock_code"], r["stock_name"], r.get("px_change_rate")) for r in loc["list"][:5]],
                "rate_mismatch_n": len(rate_err),
                "rate_mismatch_sample": rate_err[:3],
                "origin_empty": o_total is None,
            }
            report["plates"].append(entry)
            print(
                f"[{i+1}/{len(ordered)}] {code} {name}: tot {loc['total']}/{o_total} "
                f"top10_exact={top10_exact} common={top10_common} rate_bad={len(rate_err)}",
                flush=True,
            )
            await asyncio.sleep(0.35)

    report["summary"] = {
        "n": len(ordered),
        "top10_good": ok_top10,
        "total_near": near_total,
        "chip": next((p for p in report["plates"] if p["plate_code"] == "801001"), None),
        "ai": next((p for p in report["plates"] if p["plate_code"] == "803023"), None),
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("summary", report["summary"]["top10_good"], "/", report["summary"]["n"], "->", OUT, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
