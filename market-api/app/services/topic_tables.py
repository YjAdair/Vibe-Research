"""用户题材表格：原站 /wonder/topic/table 与 /wonder/topic/following。

市场题材快照走 topic_snapshots；这里是用户自建层级表，行情只读已发布收盘。
"""
from __future__ import annotations

from datetime import datetime
import math
from zoneinfo import ZoneInfo

from app.core.store import store

TZ = ZoneInfo("Asia/Shanghai")
# Explicit demo identity only; request handlers always pass an authenticated id or None.
USER = "local"
ROW_FIELDS = ("一级大类", "二级小类", "三级细分", "个股", "股票代码", "相关性", "信息源")


def _now() -> str:
    return datetime.now(TZ).isoformat()


def _iso(day: str | None) -> str | None:
    if not day:
        return None
    d = day.replace("-", "")
    if len(d) != 8 or not d.isdigit():
        raise ValueError("Invalid trade date")
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def _normalize_rows(rows: list | None) -> list[dict]:
    out = []
    for raw in rows or []:
        if not isinstance(raw, dict):
            continue
        row = {k: str(raw.get(k) or "").strip() for k in ROW_FIELDS}
        name = row["个股"] or str(raw.get("stock_name") or "").strip()
        code = row["股票代码"] or str(raw.get("symbol_code") or raw.get("stock_code") or "").strip()
        if code:
            code = code.zfill(6) if code.isdigit() else code
        row["个股"] = name
        row["股票代码"] = code
        if not name and not code:
            continue
        out.append(row)
    return out


def _member_names(rows: list[dict]) -> list[str]:
    names = []
    for row in rows:
        name = str(row.get("个股") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _latest_close_date() -> str | None:
    dates = store.daily_close_dates(1)
    return dates[0] if dates else None


_INDEX_CACHE: dict[tuple, tuple] = {}


def _close_index(date: str, days: int = 21) -> tuple[str, dict[str, dict], list[str]]:
    """收盘索引（带缓存）：daily_close_range 单次 ~0.9s，126 张表共享一份索引。"""
    cache_key = (store.path, date, days, store.kv_get("daily_close_saved_at"))
    hit = _INDEX_CACHE.get(cache_key)
    if hit:
        return hit
    rows = store.daily_close_range(date, days)
    by_date: dict[str, dict[str, dict]] = {}
    by_name: dict[str, dict] = {}
    for row in rows:
        by_date.setdefault(row["trade_date"], {})[row["stock_code"]] = row
        name = str(row.get("stock_name") or "").strip()
        if name and row["trade_date"] == date:
            by_name[name] = row
    dates = sorted(by_date)
    result = (date, {"code": by_date.get(date) or {}, "name": by_name, "all": by_date, "dates": dates}, dates)
    if len(_INDEX_CACHE) > 8:
        _INDEX_CACHE.clear()
    _INDEX_CACHE[cache_key] = result
    return result


def _chg(end: float | None, base: float | None) -> float | None:
    if not end or not base:
        return None
    return round((float(end) / float(base) - 1) * 100, 2)


def enrich_rows(rows: list[dict], date: str | None = None) -> tuple[list[dict], dict]:
    date = date or _latest_close_date()
    if not date:
        return rows, {"status": "missing", "date": None, "quote_coverage": 0}
    _, index, dates = _close_index(date, 21)
    end_map = index["all"].get(date) or {}
    dates_have = index["dates"]
    base_10 = dates_have[dates_have.index(date) - 10] if date in dates_have and dates_have.index(date) >= 10 else None
    base_20 = dates_have[dates_have.index(date) - 20] if date in dates_have and dates_have.index(date) >= 20 else None
    covered = 0
    out = []
    for row in rows:
        item = dict(row)
        code = str(item.get("股票代码") or "").zfill(6) if str(item.get("股票代码") or "").isdigit() else str(item.get("股票代码") or "")
        name = str(item.get("个股") or "").strip()
        close = end_map.get(code) if code else None
        if close is None and name:
            close = index["name"].get(name)
            if close:
                code = close.get("stock_code") or code
        if close:
            covered += 1
            last = close.get("close")
            prev = close.get("prev_close")
            item["股票代码"] = code or item.get("股票代码")
            item["symbol_code"] = code
            item["最新价"] = last
            item["涨跌幅"] = _chg(last, prev)
            item["px_change_rate"] = item["涨跌幅"]
            item["last_px"] = last
            item["10日涨幅"] = None
            item["20日涨幅"] = None
            item["return_status"] = "missing_verified_total_returns"
        out.append(item)
    stats = _stats(out)
    return out, {"status": "ok" if covered == len(rows) and covered else "partial_preview" if covered else "missing", "date": date, "quote_coverage": covered, **stats, "source": "published_daily_close"}


def _stats(rows: list[dict]) -> dict:
    pcts = [r.get("涨跌幅") for r in rows if r.get("涨跌幅") is not None]
    up = sum(1 for p in pcts if p > 0)
    down = sum(1 for p in pcts if p < 0)
    avg = round(sum(pcts) / len(pcts), 2) if pcts else None
    return {
        "today_pct": avg,
        "up_count": up if pcts else None,
        "down_count": down if pcts else None,
        "stock_count": len([r for r in rows if r.get("个股") or r.get("股票代码")]),
        "up_ratio": round(up / len(pcts) * 100, 1) if pcts else None,
        "leader_count": sum(1 for p in pcts if p > 5) if pcts else None,
    }


def _user_key(user_id: str | None) -> str | None:
    return str(user_id) if user_id is not None and str(user_id).strip() else None


def list_tables(q: str = "", include_deleted: bool = False, followed_only: bool = False, user_id: str | None = None) -> dict:
    items = store.list_topics(include_deleted=include_deleted)
    followed = set(store.followed_keys(_user_key(user_id))) if _user_key(user_id) else set()
    if followed_only:
        items = [it for it in items if it.get("unique_key") in followed]
    if q:
        needle = q.strip().lower()
        items = [it for it in items if needle in (it.get("name") or "").lower() or needle in (it.get("content") or "").lower()]
    public = []
    for it in items:
        rows, quote = enrich_rows(it.get("rows") or [])
        merged = {**it, "rows": rows, **{k: quote.get(k) for k in ("today_pct", "up_count", "down_count", "stock_count", "up_ratio", "leader_count") if k in quote}}
        public.append(store.topic_public(merged, followed=it.get("unique_key") in followed))
    pinned_order = store.kv_get("topic_pinned_order", {}) or {}
    def _sort_key(x):
        top = int(x.get("is_top") or 0)
        rank = pinned_order.get(x.get("unique_key") or "")
        return (top, -(rank if rank is not None else 10**9), x.get("updated_time") or "", x.get("name") or "")
    public.sort(key=_sort_key, reverse=True)
    return {
        "items": public,
        "data": public,
        "total": len(public),
        "status": "ok",
        "source": "user_topic_tables",
        "followed_count": len(followed),
    }


def get_table(unique_key: str, include_deleted: bool = True, user_id: str | None = None) -> dict | None:
    item = store.get_topic(unique_key, include_deleted=include_deleted)
    if not item:
        return None
    rows, quote = enrich_rows(item.get("rows") or [])
    item = {**item, "rows": rows, **{k: quote.get(k) for k in ("today_pct", "up_count", "down_count", "stock_count", "up_ratio", "leader_count") if k in quote}}
    followed = bool(_user_key(user_id) and unique_key in set(store.followed_keys(_user_key(user_id))))
    data = store.topic_public(item, followed=followed)
    data["quote"] = quote
    return data


def create_table(payload: dict, user_id: str | None = None) -> dict:
    rows = _normalize_rows(payload.get("rows"))
    now = _now()
    rec = store.upsert_topic({
        "unique_key": payload.get("unique_key"),
        "name": (payload.get("name") or "未命名题材").strip() or "未命名题材",
        "content": payload.get("content") or "",
        "rows": rows,
        "stock_count": len(_member_names(rows)),
        "reasons": payload.get("reasons") or [],
        "selection_scope": "user",
        "is_top": 0,
        "is_deleted": 0,
        "created_time": now,
        "updated_time": now,
    })
    return get_table(rec["unique_key"], user_id=user_id)


def save_table(unique_key: str, payload: dict, user_id: str | None = None) -> dict | None:
    item = store.get_topic(unique_key, include_deleted=True)
    if not item:
        return None
    rows = _normalize_rows(payload.get("rows") if "rows" in payload else item.get("rows"))
    updated = store.upsert_topic({
        **item,
        "name": (payload.get("name") if payload.get("name") is not None else item.get("name")) or item.get("name"),
        "content": payload.get("content") if payload.get("content") is not None else item.get("content"),
        "rows": rows,
        "stock_count": len(_member_names(rows)),
        "updated_time": _now(),
        "created_time": item.get("created_time") or _now(),
    })
    return get_table(updated["unique_key"], user_id=user_id)


def set_top(unique_key: str, is_top: bool, user_id: str | None = None) -> dict | None:
    item = store.mark_topic(unique_key, is_top=1 if is_top else 0, updated_time=_now())
    return get_table(unique_key, user_id=user_id) if item else None


def set_deleted(unique_key: str, deleted: bool, user_id: str | None = None) -> dict | None:
    item = store.mark_topic(unique_key, is_deleted=1 if deleted else 0, updated_time=_now())
    return get_table(unique_key, user_id=user_id) if item else None


def follows(user_id: str | None = None) -> dict:
    data = list_tables(followed_only=True, user_id=user_id)
    items = []
    for it in data["items"]:
        detail = get_table(it["unique_key"], include_deleted=False, user_id=user_id)
        if detail:
            items.append(detail)
    return {"items": items, "data": {"items": items}, "total": len(items), "status": "ok", "source": "user_topic_follows"}


def follow_status(topic_ids: list[str], user_id: str | None = None) -> dict:
    key = _user_key(user_id)
    followed = set(store.followed_keys(key)) if key else set()
    ids = [str(x) for x in topic_ids if str(x)]
    return {"followed_topic_ids": [i for i in ids if i in followed], "status": "ok"}


def toggle_follow(unique_key: str, follow: bool, user_id: str | None = None) -> dict:
    item = store.get_topic(unique_key, include_deleted=True)
    if not item:
        return {"status": "missing", "unique_key": unique_key}
    key = _user_key(user_id)
    if not key:
        return {"status": "unauthorized", "unique_key": unique_key}
    if follow:
        store.follow_topic(unique_key, key)
    else:
        store.unfollow_topic(unique_key, key)
    return {"status": "ok", "unique_key": unique_key, "is_followed": follow}


def _snapshot_rank_window(date2: str, n_days: int, db=None) -> tuple[str | None, list[str]]:
    db = db or store
    available = sorted(db.topic_snapshot_dates(370), reverse=True)
    end = _iso(date2) if date2 else (available[0] if available else None)
    if not end or (date2 and end not in available):
        return end, []
    from app.services import ml_r1_service
    calendar = [d for d in ml_r1_service.calendar_days() if d <= end]
    basis = sorted(calendar)
    selected = [d for d in basis if d <= end][-n_days:]
    return end, selected if len(selected) == n_days and all(d in available for d in selected) else []


def _compound_pct(values: list) -> float | None:
    numbers = []
    for value in values:
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value <= -100:
            return None
        numbers.append(value / 100)
    return round((math.prod(1 + value for value in numbers) - 1) * 100, 2) if numbers else None


def rank_tables(date2: str = "", n_days: int = 1, limit: int = 10, sort_by: str = "rate", store_obj=None) -> dict:
    """Aggregate only published daily values; missing days remain missing."""
    n_days = int(n_days or 1)
    store_obj = store_obj or store
    if n_days not in (1, 3, 5):
        raise ValueError("题材排行仅支持1/3/5日")
    end, dates = _snapshot_rank_window(date2, n_days, store_obj)
    if not end or len(dates) != n_days:
        return {"data": [], "items": [], "date": end, "status": "missing", "n_days": n_days,
                "sort_by": sort_by, "source": "published_topic_snapshots"}
    snapshots = {d: {str(i.get("unique_key")): i for i in store_obj.topic_snapshot_all(d)} for d in dates}
    keys = set.union(*(set(snapshots[d]) for d in dates)) if dates else set()

    ml_rows = {}
    ml_source = None
    try:
        from app.services import ml_r1_service
        n_type = 9 if sort_by == "score" else 1
        prepared = ml_r1_service.rank(15, end, n_days, n_type, 1000)
        if isinstance(prepared, list):
            ml_rows = {str(row.get("plate_code")): row for row in prepared}
            if ml_rows:
                ml_source = "ml_r1_prepared"
    except (ValueError, TypeError, KeyError):
        ml_rows = {}

    rows = []
    for key in keys:
        daily = [snapshots[d].get(key) for d in dates]
        latest = daily[-1] or {}
        prepared = ml_rows.get(key) or {}
        rates = [item.get("today_pct") if item else None for item in daily]
        scores = [item.get("score") if item else None for item in daily]
        score = prepared.get("sum_score") if prepared.get("sum_score") is not None else (
            round(sum(float(v) for v in scores) / len(scores), 2) if all(v is not None for v in scores) else None)
        sum_rate = prepared.get("sum_rate") if prepared.get("sum_rate") is not None else _compound_pct(rates)
        metric = score if sort_by == "score" else sum_rate
        row = {
            "topic_id": key,
            "topic_unique_key": key,
            "topic_name": latest.get("name"),
            "topic_content": "；".join(latest.get("reasons") or [])[:120],
            "score": score,
            "sum_rate": sum_rate,
            "limit_up_count": latest.get("limit_up_count"),
            "up_count": latest.get("up_count"),
            "down_count": latest.get("down_count"),
            "leader_count": latest.get("leader_count"),
            "trade_date": end,
            "status": "ok" if metric is not None else "missing",
            "reasons": [] if metric is not None else ["missing_window_metric"],
            "coverage": {"dates": dates, "available_days": sum(v is not None for v in (scores if sort_by == "score" else rates)), "required_days": n_days},
        }
        rows.append(row)
    rows.sort(key=lambda row: (row["status"] != "ok", -(row["score"] if sort_by == "score" and row["score"] is not None else row["sum_rate"] if row["sum_rate"] is not None else float("-inf")), row["topic_unique_key"]))
    rows = rows[:max(1, min(int(limit or 10), 200))]
    for index, row in enumerate(rows, 1):
        row["topic_id"] = index
    return {"data": rows, "items": rows, "date": end, "status": "ok" if any(r["status"] == "ok" for r in rows) else "missing",
            "n_days": n_days, "sort_by": sort_by, "source": (ml_source + "+published_topic_snapshots") if ml_source else "published_topic_snapshots"}


def topic_daily_nav(unique_key: str, date1: str = "", n: int = 30) -> dict:
    from app.services.ml_r1_service import daily_nav
    return daily_nav(15, unique_key, _iso(date1) if date1 else "", n)


def _ensure_alert_reads() -> None:
    with store._conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS topic_alert_reads_v2(user_id TEXT NOT NULL, topic_key TEXT NOT NULL, date TEXT NOT NULL, PRIMARY KEY(user_id, topic_key, date))")


def _alert_read_state(user_id: str | None) -> set[tuple[str, str]]:
    key = _user_key(user_id)
    if not key:
        return set()
    _ensure_alert_reads()
    with store._conn() as conn:
        rows = conn.execute("SELECT topic_key, date FROM topic_alert_reads_v2 WHERE user_id=?", (key,)).fetchall()
    return {(row["topic_key"], row["date"]) for row in rows}


def follows_digest(user_id: str | None = None) -> dict:
    if not _user_key(user_id):
        return {"items": [], "data": {"items": []}, "total": 0, "unread_count": 0, "raw_unread_count": 0,
                "digest_date": None, "changed_topic_count": 0, "status": "ok", "source": "topic_snapshots+daily_close"}
    from app.services import topic_follow
    key = _user_key(user_id)
    followed = store.followed_keys(key)
    dates = topic_follow._latest_dates(2)
    today_d, prev_d = (dates[0] if dates else None), (dates[1] if len(dates) > 1 else None)
    today_map = topic_follow._snapshot_map(today_d) if today_d else {}
    prev_map = topic_follow._snapshot_map(prev_d) if prev_d else {}
    read_state = _alert_read_state(key)
    rank_map = {str(item.get("unique_key")): index for index, item in enumerate(sorted(today_map.values(), key=lambda x: x.get("score") if x.get("score") is not None else float("-inf"), reverse=True), 1)}
    items, unread, changed = [], 0, 0
    for topic_key in followed:
        topic_item = store.get_topic(topic_key, include_deleted=False)
        if not topic_item:
            continue
        detail = get_table(topic_key, include_deleted=False, user_id=key)
        changes = topic_follow._daily_changes(topic_key, topic_item.get("name") or "", today_map.get(topic_key) or {}, prev_map.get(topic_key) or {}, today_d or "")
        changes = [{**change, "is_read": (topic_key, change.get("trade_date")) in read_state} for change in changes]
        unread += sum(not change["is_read"] for change in changes)
        changed += bool(changes)
        items.append({**(detail or {}), "topic_id": topic_item.get("id") or topic_key, "unique_key": topic_key,
                      "score": (today_map.get(topic_key) or {}).get("score"), "rank": rank_map.get(topic_key),
                      "today_pct": (today_map.get(topic_key) or {}).get("today_pct"), "daily_changes": changes,
                      "trade_date": today_d, "analysis_locked": True})
    return {"items": items, "data": {"items": items}, "total": len(items), "unread_count": unread,
            "raw_unread_count": unread, "digest_date": today_d, "changed_topic_count": changed,
            "status": "ok", "source": "topic_snapshots+daily_close"}


def follow_alerts(limit: int = 50, user_id: str | None = None) -> list[dict]:
    read_state = _alert_read_state(user_id)
    alerts = []
    for item in follows_digest(user_id).get("items", []):
        for change in item.get("daily_changes") or []:
            alerts.append({"id": change["id"], "topic_id": item.get("topic_id"), "unique_key": item.get("unique_key"),
                           "title": item.get("name"), "summary": change["summary"], "created_at": change.get("trade_date"),
                           "trade_date": change.get("trade_date"), "is_read": (item.get("unique_key"), change.get("trade_date")) in read_state})
    alerts.sort(key=lambda item: item.get("trade_date") or "", reverse=True)
    return alerts[:limit]


def mark_alerts_read(alert_ids: list, user_id: str | None = None) -> int:
    key = _user_key(user_id)
    if not key:
        return 0
    _ensure_alert_reads()
    count = 0
    with store._conn() as conn:
        for alert_id in alert_ids or []:
            parts = str(alert_id).split(":")
            if len(parts) >= 2 and parts[0] in set(store.followed_keys(key)) and len(parts[1]) == 10 and parts[1].count("-") == 2:
                conn.execute("INSERT OR IGNORE INTO topic_alert_reads_v2(user_id, topic_key, date) VALUES(?,?,?)", (key, parts[0], parts[1]))
                count += 1
    return count


def stock_returns(names: list[str], date: str | None = None) -> dict:
    date = date or _latest_close_date()
    if not date:
        return {"status": "missing", "items": {}, "data": {}, "note": "no published daily close"}
    rows = [{"个股": n} for n in names if str(n).strip()]
    enriched, quote = enrich_rows(rows, date)
    out = {}
    for row in enriched:
        name = row.get("个股")
        if not name:
            continue
        if not row.get("symbol_code") and row.get("涨跌幅") is None:
            continue
        out[name] = {
            "symbol_code": row.get("symbol_code") or row.get("股票代码"),
            "px_change_rate": row.get("涨跌幅"),
            "last_px": row.get("最新价"),
            "circulation_value": None,
            "px_change_rate_10d": row.get("10日涨幅"),
            "px_change_rate_20d": row.get("20日涨幅"),
        }
    return {"status": quote.get("status"), "date": date, "items": out, "data": out, "source": "published_daily_close", "quote_coverage": quote.get("quote_coverage")}


def member_codes(item: dict) -> list[dict]:
    rows, quote = enrich_rows(item.get("rows") or [])
    out = []
    for row in rows:
        code = str(row.get("symbol_code") or row.get("股票代码") or "").strip()
        name = str(row.get("个股") or "").strip() or code
        if not code and not name:
            continue
        out.append({
            "code": code,
            "name": name,
            "pct": row.get("涨跌幅"),
            "lbc": None,
            "reason": row.get("相关性") or row.get("信息源") or "",
            "level1": row.get("一级大类"),
            "level2": row.get("二级小类"),
            "level3": row.get("三级细分"),
        })
    return out
