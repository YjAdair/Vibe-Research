"""九阴九阳选股池：原站 /wonder/jyjy、/wonder/96|98|99。

公开源看不到原站完整策略参数。本地用已发布收盘复刻可观察规则：
9008 超跌反弹（昨日大跌），9016 主板，9009 人气交集。缺日保持 missing。
"""
from __future__ import annotations

from app.core.store import store
from app.services import market, popular

STRATEGIES = {
    9008: {"name": "九阴之98", "route": "98", "kind": "oversold"},
    9009: {"name": "九阴之人气", "route": "99", "kind": "popular"},
    9010: {"name": "九阴之九阳", "route": "jyjy", "kind": "oversold"},
    9016: {"name": "九阴主板", "route": "96", "kind": "mainboard"},
}


def _iso(day: str | None) -> str | None:
    if not day:
        return None
    d = str(day).replace("-", "")
    if len(d) != 8 or not d.isdigit():
        raise ValueError("Invalid trade date")
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def _pct(close, prev) -> float | None:
    if not close or not prev:
        return None
    return round((float(close) / float(prev) - 1) * 100, 2)


def _by_date(date: str, days: int = 3) -> dict[str, dict[str, dict]]:
    rows = store.daily_close_range(date, days)
    out: dict[str, dict[str, dict]] = {}
    for row in rows:
        out.setdefault(row["trade_date"], {})[row["stock_code"]] = row
    return out


def _calendar(end: str, n: int = 8) -> list[str]:
    dates = store.daily_close_dates(n + 5)
    dates = [d for d in dates if d <= end]
    return sorted(dates)[-n:]


def _is_st(name: str) -> bool:
    n = name or ""
    return "ST" in n.upper()


def _is_mainboard(code: str) -> bool:
    return bool(code) and (code.startswith("60") or code.startswith("00")) and not code.startswith("688") and not code.startswith("301")


def _is_chi_next(code: str) -> bool:
    return bool(code) and (code.startswith("30") or code.startswith("688"))


async def pool(quant_code: int | str, date: str | None = None, limit: int = 80) -> dict:
    code = int(quant_code)
    meta = STRATEGIES.get(code)
    if not meta:
        return {"quant_code": code, "status": "unsupported", "data": [], "items": [], "note": "supported 9008/9009/9010/9016"}
    day = _iso(date) if date else (store.daily_close_dates(1) or [None])[0]
    if not day:
        return {"quant_code": code, "date": None, "status": "missing", "data": [], "items": [], "name": meta["name"]}
    calendar = [d for d in reversed(store.daily_close_dates(40)) if d <= day]
    if not calendar or calendar[-1] != day:
        return {"quant_code": code, "date": day, "status": "missing", "data": [], "items": [], "name": meta["name"], "source": "published_daily_close"}
    prev = calendar[-2] if len(calendar) > 1 else None
    prev2 = calendar[-3] if len(calendar) > 2 else None
    by_date = _by_date(day, 4)
    today_map = by_date[day]
    prev_map = by_date.get(prev) or {}
    prev2_map = by_date.get(prev2) or {}
    hot = {}
    if meta["kind"] in ("popular", "oversold"):
        snap = await popular.review(day, 100)
        if snap.get("status") == "ok":
            hot = {str(r.get("symbol_code")): r for r in snap.get("items") or []}
    items = []
    ohlc_n = 0
    for stock_code, row in today_map.items():
        name = row.get("stock_name") or stock_code
        if _is_st(name):
            continue
        y = prev_map.get(stock_code)
        y2 = prev2_map.get(stock_code)
        pre1 = _pct((y or {}).get("close"), (y or {}).get("prev_close"))
        pre2 = _pct((y2 or {}).get("close"), (y2 or {}).get("prev_close"))
        open_pct = _pct(row.get("open"), row.get("prev_close"))
        low_pct = _pct(row.get("low"), row.get("prev_close"))
        if row.get("open") is not None:
            ohlc_n += 1
        keep = False
        if meta["kind"] == "oversold":
            if pre1 is None or pre1 > -7:
                continue
            if open_pct is not None and open_pct < -8:
                continue
            keep = True
        elif meta["kind"] == "mainboard":
            if not _is_mainboard(stock_code):
                continue
            if pre1 is None:
                continue
            keep = True
        elif meta["kind"] == "popular":
            if stock_code not in hot:
                continue
            keep = True
        if code == 9010 and not _is_chi_next(stock_code):
            continue
        if not keep:
            continue
        hit = hot.get(stock_code) or {}
        items.append({
            "symbol_code": stock_code,
            "symbol_name": name,
            "date": day,
            "preday": None,
            "market_cap": None,
            "pct_pre1": None if pre1 is None else round(pre1 / 100, 4),
            "pct_pre2": None if pre2 is None else round(pre2 / 100, 4),
            "open_ok": 1 if open_pct is None or open_pct >= -5 else 0,
            "m1_ok": 1,
            "low_ok": 1 if low_pct is None or low_pct > -11 else 0,
            "money_gt5_ok": None,
            "bingo_time": None,
            "bingo": 0,
            "plates": row.get("concept") or "",
            "quant_code": code,
            "status": 1,
            "rank": hit.get("rank"),
            "rank_diff": hit.get("rank_diff"),
            "is_new_high": None,
            "reason": hit.get("popularity_tag") or "",
            "last_pct": _pct(row.get("close"), row.get("prev_close")),
            "open_pct": open_pct,
            "low_pct": low_pct,
        })
    if meta["kind"] == "oversold":
        items.sort(key=lambda r: (r.get("pct_pre1") is None, r.get("pct_pre1") if r.get("pct_pre1") is not None else 0, r.get("symbol_code")))
    elif meta["kind"] == "popular":
        items.sort(key=lambda r: (r.get("rank") is None, r.get("rank") or 9999))
    else:
        items.sort(key=lambda r: (r.get("last_pct") is None, -(r.get("last_pct") or 0), r.get("symbol_code")))
    limit = max(1, min(int(limit or 80), 200))
    note = "local reconstruction from published closes; original proprietary filters are not public"
    if meta["kind"] == "oversold":
        note = "9008/9010: yesterday pct <= -7, exclude ST; today open filter only when OHLC exists"
    elif meta["kind"] == "mainboard":
        note = "9016: SSE/SZSE mainboard names from published close, not the original unpublished score"
    elif meta["kind"] == "popular":
        note = "9009: intersection of published hot list and close snapshot"
    return {
        "quant_code": code,
        "name": meta["name"],
        "route": meta["route"],
        "date": day,
        "status": "ok" if items else "empty",
        "source": "published_daily_close+popular",
        "ohlc_coverage": ohlc_n,
        "total": len(items),
        "items": items[:limit],
        "data": items[:limit],
        "note": note,
    }
