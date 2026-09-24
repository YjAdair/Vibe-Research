"""AI 盘前/盘后报告：只用已发布、通过时点校验的快照生成研报。"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.store import store
from app.services import popular, pools

TZ = ZoneInfo("Asia/Shanghai")
TYPES = {"morning": "每日盘前 (AI)", "evening": "每日盘后 (AI)"}


def _iso(day: str | None) -> str | None:
    if not day:
        return None
    d = str(day).replace("-", "")
    if len(d) != 8 or not d.isdigit():
        raise ValueError("Invalid trade date")
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def _pct(v) -> str:
    if v is None:
        return "—"
    n = float(v)
    return f"{'+' if n > 0 else ''}{n:.2f}%"


def _parse_at(value) -> datetime | None:
    if isinstance(value, datetime):
        stamp = value
    elif value:
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    return None if stamp.tzinfo is None else stamp.astimezone(TZ)


def _available_at(record: dict | None) -> str | None:
    """Local availability is distinct from the provider's market timestamp."""
    if not record:
        return None
    return record.get("available_at") or record.get("collected_at")


def _close_meta(day: str) -> dict | None:
    meta = store.daily_close_run(day)
    if meta and meta.get("complete") is True:
        return meta
    return None


def _latest_date() -> str | None:
    for day in store.daily_close_dates(370):
        if _close_meta(day):
            return day
    return None


def _previous_completed_date(day: str) -> str | None:
    calendar = store.kv_get('collector_calendar_v1', {}) or {}
    if not (calendar.get('complete') is True or calendar.get('verified_at')):
        return None
    days = sorted({_iso(d) for d in calendar.get('days', [])})
    if day not in days or days.index(day) == 0:
        return None
    previous = days[days.index(day)-1]
    return previous if _close_meta(previous) else None


def _validate_record(name: str, record: dict | None, day: str, cutoff: datetime) -> tuple[dict | None, str | None]:
    if not isinstance(record, dict):
        return None, f"missing_{name}"
    if (record.get("date") or record.get("trade_date")) != day:
        return None, f"date_mismatch_{name}"
    if not record.get("source"):
        return None, f"unverified_source_{name}"
    if record.get("complete") is not True:
        return None, f"incomplete_{name}"
    field = 'pool' if name in ('up', 'down', 'broken') else 'items' if name == 'popular' else None
    if field and (not isinstance(record.get(field), list) or record.get('total') != len(record[field])):
        return None, f'incomplete_rows_{name}'
    available = _available_at(record)
    if not available or _parse_at(available) is None:
        return None, f"unverified_{name}"
    if _parse_at(available) > cutoff:
        return None, f"future_available_at_{name}"
    source_at = _parse_at(record.get('source_as_of'))
    if source_at is None or source_at.date().isoformat() != day or source_at.hour < 15:
        return None, f"unverified_close_time_{name}"
    if source_at > _parse_at(available):
        return None, f"invalid_time_order_{name}"
    return record, None


def _validate_topics(items: list[dict], day: str, cutoff: datetime) -> tuple[list[dict], str | None, str | None]:
    if not items:
        return [], "missing_topics", None
    available = []
    for item in items:
        if item.get('trade_date') != day or item.get('complete') is not True or not item.get('source'):
            return [], 'unverified_topics', None
        stamp = _available_at(item)
        parsed = _parse_at(stamp)
        if not stamp or parsed is None:
            return [], "unverified_topics", None
        if parsed > cutoff:
            return [], "future_available_at_topics", None
        available.append((parsed, stamp))
    return items, None, max(available)[1]


def _topic_rank(date: str, limit: int = 8) -> list[dict]:
    items = store.topic_snapshot_all(date)
    rows = []
    for it in items:
        rows.append({
            "name": it.get("name"),
            "unique_key": it.get("unique_key"),
            "score": round(it["score"]) if it.get("score") is not None else None,
            "pct": it.get("today_pct"),
            "limit_up_count": it.get("limit_up_count") if it.get("limit_up_count") is not None else None,
        })
    rows.sort(key=lambda x: (x["pct"] is None, -(x["pct"] or 0), -(x["score"] or 0)))
    return rows[:limit]


async def build(report_type: str, date: str | None = None) -> dict:
    kind = "evening" if report_type in ("evening", "night", "after") else "morning"
    day = _iso(date) if date else (datetime.now(TZ).date().isoformat() if kind == "morning" else _latest_date())
    if not day:
        return {"status": "missing", "type": kind, "items": []}
    now = datetime.now(TZ)
    session_cutoff = datetime.fromisoformat(f"{day}T{'09:30:00' if kind == 'morning' else '23:59:59'}+08:00")
    cutoff = min(now, session_cutoff)
    evidence_day = _previous_completed_date(day) if kind == "morning" else day
    reasons = []
    if day > now.date().isoformat() or (kind == 'evening' and cutoff.hour < 15):
        reasons.append('session_not_completed')
    evidence = []
    close = _close_meta(evidence_day) if evidence_day else None
    close, error = _validate_record("daily_close", close, evidence_day or day, cutoff)
    if error:
        reasons.append(error)
    elif close:
        evidence.append({"name": "daily_close", "source": close.get("source") or "published_daily_close",
                         "trade_date": evidence_day, "available_at": _available_at(close)})

    snapshots = {}
    for name in ("up", "down", "broken"):
        snap = pools.published(name, evidence_day) if evidence_day else None
        snap, error = _validate_record(name, snap, evidence_day or day, cutoff)
        if error:
            reasons.append(error)
        else:
            snapshots[name] = snap
            evidence.append({"name": f"pool_{name}", "source": snap.get("source") or "published_limit_pool",
                             "trade_date": evidence_day, "available_at": _available_at(snap)})

    raw_topics = store.topic_snapshot_all(evidence_day) if evidence_day else []
    raw_topics, error, topics_available = _validate_topics(raw_topics, evidence_day or day, cutoff)
    if error:
        reasons.append(error)
    else:
        evidence.append({"name": "topic_snapshot", "source": "published_topic_snapshot",
                         "trade_date": evidence_day, "available_at": topics_available,
                         "input_count": len(raw_topics)})
    topics = sorted([{'name': it.get('name'), 'unique_key': it.get('unique_key'),
                      'score': it.get('score'), 'pct': it.get('today_pct'),
                      'limit_up_count': it.get('limit_up_count')} for it in raw_topics],
                    key=lambda r: (r['pct'] is None, -(r['pct'] or 0)))[:8]

    pop = popular.published(evidence_day) if evidence_day else None
    pop, error = _validate_record("popular", pop, evidence_day or day, cutoff)
    if error:
        reasons.append(error)
    else:
        evidence.append({"name": "popular", "source": pop.get("source") or "published_popular",
                         "trade_date": evidence_day, "available_at": _available_at(pop)})

    reasons = list(dict.fromkeys(reasons))
    concepts = [t["name"] for t in topics if t.get("name")][:6]
    zt = len(snapshots["up"].get("pool") or []) if "up" in snapshots else None
    dt = len(snapshots["down"].get("pool") or []) if "down" in snapshots else None
    zb = len(snapshots["broken"].get("pool") or []) if "broken" in snapshots else None
    title = ("【AI盘后】" if kind == "evening" else "【AI盘前】") + f"{day} 市场结构"
    lines = []
    lines.append(f"{'盘后复盘' if kind == 'evening' else '盘前观察'}：{day}")
    fmt_count = lambda value: "—" if value is None else str(value)
    lines.append(f"涨停 {fmt_count(zt)} 家，跌停 {fmt_count(dt)} 家，炸板 {fmt_count(zb)} 家。")
    if topics:
        lines.append("概念：" + "、".join(f"{t['name']}({_pct(t['pct'])}，涨停{fmt_count(t['limit_up_count'])})" for t in topics[:5]))
        lines.append("核心逻辑：以上题材来自已发布东财概念快照与涨停池聚合，不是盘中实时主题挖掘。")
        leaders = []
        for t in topics[:3]:
            snap = next((it for it in raw_topics if it.get("unique_key") == t["unique_key"]), None)
            stocks = (snap or {}).get("stocks") or []
            if stocks:
                s = stocks[0]
                leaders.append(f"[{s.get('name')}({s.get('code')}) {_pct(s.get('pct'))}]")
        if leaders:
            lines.append("核心股票: " + " ".join(leaders))
    hot_items = pop.get("items") or [] if pop else []
    if pop and hot_items:
        lines.append("人气：" + "、".join(f"{s.get('symbol_name')}#{s.get('rank')}" for s in hot_items[:5]))
    if reasons:
        lines.append("报告输入缺失：" + "、".join(reasons))
    content = "<br>".join(lines)
    payload = {
        "id": f"{kind}:{day}",
        "type": kind,
        "title": title,
        "created_time": now.isoformat(),
        "trade_date": day,
        "data_as_of": evidence_day,
        "cutoff": cutoff.isoformat(),
        "verification_version": "ai_report_safety_v2",
        "source_evidence": evidence,
        "concepts": concepts,
        "content": content,
        "require_subscription": False,
        "source": "published_snapshots",
        "status": "ok" if not reasons else "missing_input",
        "ztjs": zt,
        "df_num": dt,
        "zb_num": zb,
    }
    return store.ai_report_save(kind, day, payload)


def _read_status(item: dict) -> tuple[str, bool]:
    if item.get("verification_version") != "ai_report_safety_v2":
        return "legacy_unverified", True
    status = item.get("status") or "missing_input"
    return status, False


async def list_reports(report_type: str = "morning", page: int = 1, page_size: int = 20) -> dict:
    kind = "evening" if report_type in ("evening", "night", "after") else "morning"
    items = store.ai_report_list(kind)
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 20), 50))
    start = (page - 1) * page_size
    chunk = items[start:start + page_size]
    public = [{
        "id": it.get("id"),
        "title": it.get("title"),
        "created_time": it.get("created_time"),
        "concepts": it.get("concepts") or [],
        "type": it.get("type"),
        "trade_date": it.get("trade_date"),
        "status": _read_status(it)[0],
        "legacy_unverified": _read_status(it)[1],
    } for it in chunk]
    return {"items": public, "total": len(items), "page": page, "page_size": page_size, "type": kind, "status": "ok" if items else "missing"}


async def detail(report_id: str) -> dict:
    item = store.ai_report_get(report_id)
    if not item:
        return {"status": "missing", "id": report_id}
    status, legacy = _read_status(item)
    return {**item, "content": "历史报告缺少可验证的时点与输入证据" if legacy else item.get("content"), "status": status, "legacy_unverified": legacy,
            "complete": status == "ok", "require_subscription": False,
            "liked_by_me": False, "is_shared": False}
