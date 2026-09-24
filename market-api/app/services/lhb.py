"""龙虎榜：按交易日发布完整快照，读路径默认不打上游。"""
from __future__ import annotations
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from app.core.store import store
from app.datasources import eastmoney

TZ = ZoneInfo("Asia/Shanghai")


def _map(row: dict) -> dict:
    return {
        "stock_code": row.get("SECURITY_CODE", ""),
        "stock_name": row.get("SECURITY_NAME_ABBR", ""),
        "close_price": row.get("CLOSE_PRICE"),
        "change_rate": row.get("CHANGE_RATE"),
        "turnover_rate": row.get("TURNOVERRATE"),
        "explanation": row.get("EXPLANATION", ""),
        "explain": row.get("EXPLAIN", ""),
        "buy_amount": row.get("BILLBOARD_BUY_AMT"),
        "sell_amount": row.get("BILLBOARD_SELL_AMT"),
        "net_amount": row.get("BILLBOARD_NET_AMT"),
        "deal_amount_ratio": row.get("DEAL_AMOUNT_RATIO"),
        "free_market_cap": row.get("FREE_MARKET_CAP"),
        "trade_date": row.get("TRADE_DATE"),
        "d1_return": row.get("D1_CLOSE_ADJCHRATE"),
        "d2_return": row.get("D2_CLOSE_ADJCHRATE"),
    }


def iso(day: str) -> str:
    d = day.replace("-", "")
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


async def collect(day: str) -> dict:
    date = iso(day)
    first = await eastmoney.lhb_detail(date, page=1, size=200)
    pages = int(first.get("pages") or 1)
    rows = list(first.get("data") or [])
    for page in range(2, pages + 1):
        nxt = await eastmoney.lhb_detail(date, page=page, size=200)
        if int(nxt.get("pages") or 0) != pages:
            raise ValueError("LHB page count changed during pagination")
        chunk = nxt.get("data") or []
        if not chunk:
            raise ValueError("LHB truncated")
        rows.extend(chunk)
    items = [_map(r) for r in rows]
    codes = [i["stock_code"] for i in items]
    if any(len(c) != 6 or not c.isdigit() for c in codes):
        raise ValueError("Invalid LHB stock code")
    if len(set(codes)) != len(codes):
        # 同一股票可能因不同上榜理由出现多次，按代码+理由去重后仍允许重复代码。
        keys = [(i["stock_code"], i.get("explanation") or i.get("explain") or "") for i in items]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate LHB rows")
    if not items:
        # 东财龙虎榜盘后延迟发布（通常 17:00 后）。空结果视为未发布，
        # 不落库、任务保持可重试，避免空快照被标记 success 后当日真实数据永远缺失。
        raise ValueError("LHB not published yet (empty result)")
    payload = {
        "date": date,
        "complete": True,
        "total": len(items),
        "pages": pages,
        "items": items,
        "source": "eastmoney_dailybillboard",
        "collected_at": datetime.now(TZ).isoformat(),
        "empty_ok": True,
    }
    store.lhb_save(date, payload)
    return payload


def published(day: str) -> dict | None:
    return store.lhb_get(iso(day))


async def lhb_list(date: str) -> list[dict]:
    snap = published(date)
    if snap:
        return snap["items"]
    if settings.collector_mode == "embedded":
        return (await collect(date))["items"]
    return []


def _seat_row(raw: dict, side: int) -> dict:
    return {
        "trade_date": (raw.get("TRADE_DATE") or "")[:10],
        "stock_code": raw.get("SECURITY_CODE") or "",
        "side": side,  # 1=买方 2=卖方（原站 traders.type 契约）
        "dept_code": raw.get("OPERATEDEPT_CODE") or "",
        "dept_name": raw.get("OPERATEDEPT_NAME") or "",
        "reason": raw.get("EXPLANATION") or "",
        "buy_amount": raw.get("BUY"),
        "sell_amount": raw.get("SELL"),
        "net_amount": raw.get("NET"),
        "rank": None,
        "win_rate_3d": raw.get("RISE_PROBABILITY_3DAY"),
    }


async def collect_seats(date: str) -> list[dict]:
    """拉当日买卖两侧全部席位明细（每侧约 114-170 页 x 200 条）。"""
    rows: list[dict] = []
    for side, key in ((1, "buy"), (2, "sell")):
        first = await eastmoney.lhb_seat_details(date, key, page=1, size=200)
        pages = int(first.get("pages") or 0)
        chunk = [_seat_row(r, side) for r in (first.get("data") or [])]
        rows.extend(chunk)
        for page in range(2, pages + 1):
            nxt = await eastmoney.lhb_seat_details(date, key, page=page, size=200)
            if int(nxt.get("pages") or 0) != pages:
                raise ValueError("LHB seats page count changed during pagination")
            rows.extend(_seat_row(r, side) for r in (nxt.get("data") or []))
    if not rows:
        raise ValueError("LHB seats not published yet (empty result)")
    return rows


def _rank_seats(rows: list[dict]) -> list[dict]:
    """按 (stock, side) 内买卖额降序补 rank（原站 traders.rank 契约）。"""
    groups: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        groups.setdefault((r["stock_code"], r["side"]), []).append(r)
    for (code, side), grp in groups.items():
        amt_key = "buy_amount" if side == 1 else "sell_amount"
        grp.sort(key=lambda r: -(r.get(amt_key) or 0))
        for i, r in enumerate(grp, 1):
            r["rank"] = i
    return rows


def publish_seats(date: str, rows: list[dict]) -> None:
    store.lhb_seats_save(date, _rank_seats(rows))


async def collect_with_seats(day: str) -> dict:
    """个股快照 + 席位明细一并采集（collect 兼容入口）。"""
    payload = await collect(day)
    date = iso(day)
    seats = await collect_seats(date)
    publish_seats(date, seats)
    payload["seats_total"] = len(seats)
    return payload


def stock_detail(date: str, stock_code: str) -> dict:
    """原站 /market/lhb/detail 契约：{detail, traders}。"""
    snap = published(date)
    items = [i for i in (snap["items"] if snap else []) if i["stock_code"] == stock_code]
    seats = store.lhb_seats_get(date, stock_code)
    if not items and not seats:
        return {"detail": None, "traders": []}
    base = items[0] if items else {}
    detail = {
        "cur_price": base.get("close_price"),
        "quote_change": base.get("change_rate"),
        "turnover_ratio": base.get("turnover_rate"),
        "buy_in": base.get("buy_amount"),
        "up_reason": base.get("explanation") or "",
        "join_num": len(seats),
    }
    traders = [
        {
            "trader_id": r["dept_code"],
            "trader_name": r["dept_name"],
            "type": r["side"],
            "rank": r["rank"],
            "buy_amount": r["buy_amount"] or 0,
            "sell_amount": r["sell_amount"] or 0,
            "reason_type": "default",
            "win_rate_3d": r["win_rate_3d"],
        }
        for r in seats
    ]
    return {"detail": detail, "traders": traders}


def trader_history(dept_code: str, page: int = 1, per_page: int = 20, stock_code: str | None = None) -> dict:
    """原站 /market/lhb/trader/history 契约：{list, total}。"""
    rows, total = store.lhb_seats_by_dept(dept_code, page, per_page, stock_code)
    # 个股名/涨跌幅/换手/收盘/成交额不在 lhb_seats 表，从同日 lhb 快照 join（覆盖度 100%）
    quote: dict[str, dict] = {}
    for d in {r["trade_date"] for r in rows}:
        for i in (published(d) or {}).get("items", []):
            quote[i["stock_code"]] = i
    def _row(r: dict) -> dict:
        q = quote.get(r["stock_code"]) or {}
        return {
            "date": r["trade_date"],
            "stock_code": r["stock_code"],
            "stock_name": q.get("stock_name"),
            "change_rate": q.get("change_rate"),
            "close_price": q.get("close_price"),
            "turnover_rate": q.get("turnover_rate"),
            "turnover_amount": (q.get("buy_amount") or 0) + (q.get("sell_amount") or 0) if q else None,
            "trader_name": r["dept_name"],
            "type": r["side"],
            "rank": r["rank"],
            "buy_amount": r["buy_amount"] or 0,
            "sell_amount": r["sell_amount"] or 0,
            "win_rate_3d": r["win_rate_3d"],
        }

    return {"list": [_row(r) for r in rows], "total": total}
