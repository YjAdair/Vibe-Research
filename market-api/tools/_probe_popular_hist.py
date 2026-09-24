"""Probe free historical popularity endpoints. Read-only; no DB write."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parents[2] / "docs" / "_probe_popular_hist_sources.json"
EM_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://vipmoney.eastmoney.com",
    "Referer": "https://vipmoney.eastmoney.com/",
}
BASE = {
    "appId": "appId01",
    "globalId": "786e4c21-70dc-435a-93bb-38",
    "marketType": "",
}


async def post(client: httpx.AsyncClient, path: str, body: dict) -> dict:
    url = f"https://emappdata.eastmoney.com/stockrank/{path}"
    r = await client.post(url, json=body, headers=EM_HEADERS, timeout=30)
    try:
        j = r.json()
    except Exception:
        return {"_http": r.status_code, "_text": (r.text or "")[:300]}
    return {"_http": r.status_code, **j}


def summarize(j: dict) -> dict:
    data = j.get("data")
    out = {
        "http": j.get("_http"),
        "code": j.get("code"),
        "message": j.get("message") or j.get("msg"),
        "data_type": type(data).__name__,
    }
    if isinstance(data, list) and data:
        out["n"] = len(data)
        out["keys"] = sorted({k for row in data[:20] for k in (row or {}).keys()})
        out["sample"] = data[0]
        out["sample_last"] = data[-1]
    elif isinstance(data, dict):
        out["data_keys"] = sorted(data.keys())
        rows = data.get("list") or data.get("rankList") or data.get("data") or []
        if isinstance(rows, list) and rows:
            out["n"] = len(rows)
            out["keys"] = sorted({k for row in rows[:20] for k in (row or {}).keys()})
            out["sample"] = rows[0]
    else:
        out["snippet"] = str(j)[:400]
    return out


async def main() -> None:
    report: dict = {}
    async with httpx.AsyncClient() as client:
        # current top (known working)
        report["getAllCurrentList"] = summarize(
            await post(client, "getAllCurrentList", {**BASE, "pageNo": 1, "pageSize": 100})
        )

        # candidate full-board history endpoints
        for path, extra in [
            ("getAllHisRcList", {"pageNo": 1, "pageSize": 100}),
            ("getAllHisRcList", {"pageNo": 1, "pageSize": 100, "day": 1}),
            ("getAllHisList", {"pageNo": 1, "pageSize": 100}),
            ("getHistoryCurrentList", {"pageNo": 1, "pageSize": 100}),
            ("getHisRankList", {"pageNo": 1, "pageSize": 100}),
            # dated variants guessed from blogs
            ("getAllHisRcList", {"pageNo": 1, "pageSize": 50, "rankType": "current"}),
        ]:
            key = f"{path}_{json.dumps(extra, sort_keys=True)}"
            try:
                report[key] = summarize(await post(client, path, {**BASE, **extra}))
            except Exception as exc:
                report[key] = {"error": type(exc).__name__, "msg": str(exc)[:200]}
            await asyncio.sleep(0.4)

        # per-stock history (known in akshare)
        report["getHisList_SZ000001"] = summarize(
            await post(
                client,
                "getHisList",
                {**BASE, "srcSecurityCode": "SZ000001", "yearType": "1"},
            )
        )

        # THS public with date param (known broken for hist)
        for url in [
            "https://eq.10jqka.com.cn/open/api/hot_list/v1/hot_stock/a/hour/data.txt",
            "https://eq.10jqka.com.cn/open/api/hot_list/v1/hot_stock/a/day/data.txt",
            "https://eq.10jqka.com.cn/open/api/hot_list/v1/hot_stock/a/hour/data.txt?date=20260918",
            "https://eq.10jqka.com.cn/open/api/hot_list/v1/hot_stock/a/day/data.txt?date=2026-09-18",
        ]:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://eq.10jqka.com.cn/"}, timeout=20)
            try:
                j = r.json()
            except Exception:
                report[url] = {"http": r.status_code, "text": (r.text or "")[:120]}
                continue
            rows = (((j.get("data") or {}).get("stock_list")) or [])
            top = [x.get("code") for x in rows[:3]]
            report[url] = {"http": r.status_code, "n": len(rows), "top3": top, "status": j.get("status_code")}

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", OUT)
    for k, v in report.items():
        brief = {kk: vv for kk, vv in v.items() if kk not in ("sample", "sample_last")}
        print(k[:80], "=>", json.dumps(brief, ensure_ascii=False)[:220])


if __name__ == "__main__":
    asyncio.run(main())
