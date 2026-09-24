# -*- coding: utf-8 -*-
"""我的题材（follows）与关注提醒（follow-alerts）：原站 chunk-0570052c 口径。

- /v3/topic/follows：关注题材摘要卡（score/rank/均涨/核心股/每日变化/持续性）
- /v3/topic/follow-alerts：最近变化提醒（title/summary/is_read）
- /v3/topic/follow-alerts/read：标记已读
- /v3/topic/table/{id}/continuity：VIP 持续性卡片

数据全部只读已发布题材快照与 daily_close，不现场拉上游。
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.store import store

TZ = ZoneInfo("Asia/Shanghai")


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _snapshot_map(date: str) -> dict[str, dict]:
    return {str(i.get("unique_key")): i for i in store.topic_snapshot_all(date)}


def _latest_dates(limit: int = 2) -> list[str]:
    dates = store.topic_snapshot_dates(limit)
    return list(dates)


def _core_stocks(topic: dict, stats_rows: list[dict], limit: int = 3) -> list[dict]:
    """核心股：成分股中当日涨幅最高者（只读已发布收盘，行情字段由前端 stock-returns 补）。"""
    seen: set[str] = set()
    unique_rows = []
    for r in stats_rows:
        nm = str(r.get("个股") or "").strip()
        if not nm or nm in seen:
            continue
        seen.add(nm)
        unique_rows.append(r)
    ranked = sorted(
        unique_rows,
        key=lambda r: float(r.get("涨跌幅") or -999),
        reverse=True,
    )
    return [
        {
            "code": str(r.get("股票代码") or ""),
            "name": r.get("个股"),
            "pct": r.get("涨跌幅"),
            "quote_date": r.get("_date"),
            "quote_time": "15:00",
        }
        for r in ranked[:limit]
    ]


def _daily_changes(unique_key: str, name: str, today: dict, prev: dict | None, digest_date: str) -> list[dict]:
    """每日变化：用当日 vs 前一份快照的统计差生成摘要（原站为编辑/评分提醒，本地方差值提醒）。"""
    changes: list[dict] = []
    if not today or not prev:
        return changes
    diffs = []
    tp, pp = today.get("today_pct"), prev.get("today_pct")
    if tp is not None and pp is not None and abs(float(tp) - float(pp)) >= 1.0:
        diffs.append(f"成分股均涨 {pp}% → {tp}%")
    lu, plu = today.get("limit_up_count" or 0), prev.get("limit_up_count" or 0)
    if lu is not None and plu is not None and int(lu) != int(plu):
        diffs.append(f"涨停 {plu} 家 → {lu} 家")
    ur, pur = today.get("up_ratio"), prev.get("up_ratio")
    if ur is not None and pur is not None and abs(float(ur) - float(pur)) >= 10.0:
        diffs.append(f"上涨占比 {pur}% → {ur}%")
    if not diffs:
        return changes
    changes.append({
        "id": f"{unique_key}:{digest_date}:stats",
        "trade_date": digest_date,
        "summary": "；".join(diffs),
        "alert_count": 1,
        "is_read": _is_read(unique_key, digest_date),
    })
    return changes


def _read_state() -> set[str]:
    """已读状态存 daily_close_runs 风格的 KV（沿用 sqlite 简单表）。"""
    with store._conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS topic_alert_reads("
            "topic_key TEXT, date TEXT, PRIMARY KEY(topic_key, date))"
        )
        rows = conn.execute("SELECT topic_key, date FROM topic_alert_reads").fetchall()
    return {(r["topic_key"], r["date"]) for r in rows}


def _is_read(topic_key: str, date: str) -> bool:
    with store._conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS topic_alert_reads("
            "topic_key TEXT, date TEXT, PRIMARY KEY(topic_key, date))"
        )
        row = conn.execute(
            "SELECT 1 FROM topic_alert_reads WHERE topic_key=? AND date=?", (topic_key, date)
        ).fetchone()
    return bool(row)


def _mark_read(topic_key: str, date: str) -> None:
    with store._conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS topic_alert_reads("
            "topic_key TEXT, date TEXT, PRIMARY KEY(topic_key, date))"
        )
        conn.execute(
            "INSERT OR IGNORE INTO topic_alert_reads(topic_key, date) VALUES(?,?)",
            (topic_key, date),
        )


def follows_digest() -> dict:
    """关注题材摘要：items + unread_count/digest_date/changed_topic_count。"""
    from app.services import topic_tables

    followed = store.followed_keys("local")
    dates = _latest_dates(2)
    today_d, prev_d = (dates[0] if dates else None), (dates[1] if len(dates) > 1 else None)
    today_map = _snapshot_map(today_d) if today_d else {}
    prev_map = _snapshot_map(prev_d) if prev_d else {}
    read_state = _read_state()

    # rank：当日全部快照按 score 排名
    ranked = sorted(today_map.values(), key=lambda x: x.get("score") or 0, reverse=True)
    rank_map = {str(i.get("unique_key")): n for n, i in enumerate(ranked, 1)}

    items = []
    changed = 0
    unread = 0
    for key in followed:
        t = store.get_topic(key, include_deleted=False)
        if not t:
            continue
        snap_today = today_map.get(key)
        snap_prev = prev_map.get(key)
        detail = topic_tables.get_table(key, include_deleted=False)
        rows = (detail or {}).get("rows") or []
        core = _core_stocks(t, rows)
        changes = _daily_changes(key, t.get("name") or "", snap_today or {}, snap_prev or {}, today_d or "")
        for c in changes:
            if (key, c.get("trade_date")) not in read_state:
                unread += 1
        if changes:
            changed += 1
        items.append({
            **(detail or {}),
            "topic_id": t.get("id") or key,
            "unique_key": key,
            "score": (snap_today or {}).get("score"),
            "rank": rank_map.get(key),
            "today_pct": (snap_today or {}).get("today_pct"),
            "core_stocks": core,
            "daily_changes": changes,
            "has_update": bool((t.get("updated_time") or "") >= (today_d or "")),
            "trade_date": today_d,
            "continuity": _continuity(key),
            "analysis_locked": True,
        })
    return {
        "items": items,
        "data": {"items": items},
        "total": len(items),
        "unread_count": unread,
        "raw_unread_count": unread,
        "digest_date": today_d,
        "changed_topic_count": changed,
        "status": "ok",
        "source": "topic_snapshots+daily_close",
    }


def _continuity(unique_key: str, days: int = 5) -> dict:
    """持续性（VIP 卡片）：最近 N 份快照的 score/today_pct 序列状态判断。"""
    dates = store.topic_snapshot_dates(days)
    seq = []
    for d in dates:
        snap = next((i for i in store.topic_snapshot_all(d) if str(i.get("unique_key")) == unique_key), None)
        if snap:
            snap = dict(snap, trade_date=d)
            seq.append(snap)
    if len(seq) < 2:
        return {"available": False, "state": "", "continuity_score": None, "state_summary": "历史评分不足，暂不形成持续判断。"}
    pcts = [float(s.get("today_pct") or 0) for s in seq]
    scores = [float(s.get("score") or 0) for s in seq]
    up_days = sum(1 for p in pcts if p > 0)
    avg = sum(pcts) / len(pcts)
    if up_days >= len(pcts) - 1 and avg > 2:
        state, summary = "延续增强", "连续多日上涨且均涨幅维持高位。"
    elif up_days >= len(pcts) * 0.6 and avg > 0.5:
        state, summary = "持续稳定", "涨多跌少，题材热度仍在延续。"
    elif pcts[-1] < 0 and sum(1 for p in pcts if p > 0) <= len(pcts) / 2:
        state, summary = "退潮迹象", "多数交易日收跌，注意兑现节奏。"
    else:
        state, summary = "高位分歧", "涨跌互现，进入分歧阶段。"
    return {
        "available": True,
        "state": state,
        "continuity_score": round(sum(scores) / len(scores), 1),
        "state_summary": summary,
        "dates": [s.get("trade_date") for s in seq],
        "pcts": pcts,
    }


def follow_alerts(limit: int = 50) -> list[dict]:
    """最近变化提醒列表（原站为流式提醒，本地由快照差值生成）。"""
    digest = follows_digest()
    alerts = []
    read_state = _read_state()
    for it in digest["items"]:
        for c in it.get("daily_changes") or []:
            alerts.append({
                "id": c["id"],
                "topic_id": it.get("topic_id"),
                "unique_key": it.get("unique_key"),
                "title": it.get("name"),
                "summary": c["summary"],
                "created_at": c.get("trade_date"),
                "trade_date": c.get("trade_date"),
                "is_read": (it.get("unique_key"), c.get("trade_date")) in read_state,
            })
    alerts.sort(key=lambda a: a.get("trade_date") or "", reverse=True)
    return alerts[:limit]


def mark_alerts_read(alert_ids: list) -> int:
    """标记提醒已读；同时支持 topic_key:date 形式的 id。"""
    n = 0
    for aid in alert_ids or []:
        s = str(aid)
        parts = s.split(":")
        # id 形如 unique_key[:date[:suffix]]；key 本身是 32 位 hex，不含冒号
        if len(parts) >= 2 and parts[0] and len(parts[0]) == 32:
            date = parts[1]
            if len(date) == 10 and date.count("-") == 2:
                _mark_read(parts[0], date)
                n += 1
    return n
