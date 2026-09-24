"""题材榜刷新：今日预览 → 今日定稿 → 历史只补缺。

页面只读本地。实时榜不带日期，会话日由本地交易日历定。
历史日不能用实时榜回填。
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from app.core.store import store
from app.datasources import kaipanla_plate

TZ = ZoneInfo("Asia/Shanghai")
PLATE_TYPE = 17
MIN_ROWS = 100
SOURCE = "kaipanla_zhishu_ranking"
# 盘中超过这个时间没采到，标 stale
INTRADAY_STALE_SECONDS = 10 * 60
# 收盘后未定稿超过这个时间，标 stale
POSTCLOSE_STALE_SECONDS = 2 * 60 * 60


def fingerprint(rows: list[dict], top_n: int = 12) -> str:
    ranked = sorted(
        (r for r in rows if isinstance(r.get("score"), (int, float))),
        key=lambda r: (-float(r["score"]), r.get("plate_code") or ""),
    )[:top_n]
    return "|".join(f"{r.get('plate_code')}:{int(float(r['score']))}" for r in ranked)


def _meta_for(day: str) -> dict:
    meta = store.plate_rank_day_meta_get(PLATE_TYPE, day)
    if meta:
        return meta
    count = store.plate_rank_day_count(PLATE_TYPE, day)
    if count <= 0:
        return {"status": "missing_input", "source": None, "collected_at": None, "row_count": 0, "fingerprint": None}
    # 旧快照没有 meta：过去交易日当定稿，当天当预览
    today = datetime.now(TZ).date().isoformat()
    status = "partial_preview" if day == today else "final"
    return {"status": status, "source": SOURCE, "collected_at": None, "row_count": count, "fingerprint": None}


def day_meta(day: str) -> dict:
    return _meta_for(day)


def _past_complete_as_final(day: str, meta: dict, *, now: datetime | None = None) -> bool:
    """过去交易日已有齐套快照时，读侧视同定稿。

    收盘后若只写成 partial_preview 未翻 final，3/5 日窗口会整列变空；
    库里日强度已齐时不应再挡多日求和。当天预览仍按预览处理。
    """
    now = now or datetime.now(TZ)
    today = now.astimezone(TZ).date().isoformat()
    if day >= today:
        return False
    return int(meta.get("row_count") or 0) >= MIN_ROWS


def day_status(day: str, *, now: datetime | None = None) -> str:
    """读侧状态。可能把过期预览标成 stale。"""
    now = now or datetime.now(TZ)
    meta = _meta_for(day)
    status = meta.get("status") or "missing_input"
    if status == "missing_input":
        return status
    if status == "final" or _past_complete_as_final(day, meta, now=now):
        return "final"
    collected = meta.get("collected_at")
    if not collected:
        return "stale" if status == "partial_preview" else status
    try:
        at = datetime.fromisoformat(str(collected))
        if at.tzinfo is None:
            at = at.replace(tzinfo=TZ)
    except ValueError:
        return "stale"
    age = (now - at).total_seconds()
    t = now.astimezone(TZ).time()
    if dtime(9, 30) <= t <= dtime(15, 0):
        if age > INTRADAY_STALE_SECONDS:
            return "stale"
    elif t > dtime(15, 0) and status == "partial_preview" and age > POSTCLOSE_STALE_SECONDS:
        return "stale"
    return status


def is_final(day: str, *, now: datetime | None = None) -> bool:
    meta = _meta_for(day)
    if meta.get("status") == "final":
        return True
    return _past_complete_as_final(day, meta, now=now)


async def _publish(day: str, rows: list[dict], status: str) -> dict:
    result = await kaipanla_plate._publish(day, rows, status)  # noqa: SLF001
    store.plate_rank_day_meta_save(
        PLATE_TYPE,
        day,
        status=status,
        source=SOURCE,
        collected_at=result["collected_at"],
        row_count=result["rows"],
        fingerprint=fingerprint(rows),
    )
    result["status"] = status
    result["fingerprint"] = fingerprint(rows)
    return result


async def refresh_intraday(day: str | None = None, *, now: datetime | None = None) -> dict:
    """盘中刷新今日列。已定稿则跳过。"""
    now = now or datetime.now(TZ)
    day = day or now.date().isoformat()
    meta = _meta_for(day)
    if meta.get("status") == "final":
        return {"date": day, "skipped": "final", "status": "final", "rows": meta.get("row_count") or 0}
    rows = await kaipanla_plate.fetch_realtime_rank()
    if len(rows) < MIN_ROWS:
        raise ValueError(f"incomplete realtime plate rank: {len(rows)}")
    return await _publish(day, rows, "partial_preview")


async def finalize_today(day: str | None = None, *, now: datetime | None = None) -> dict:
    """收盘定稿。连续两次 Top12 指纹相同，或已过 16:00 且有齐套快照，标 final。"""
    now = now or datetime.now(TZ)
    day = day or now.date().isoformat()
    meta = _meta_for(day)
    if meta.get("status") == "final":
        return {"date": day, "skipped": "already_final", "status": "final", "rows": meta.get("row_count") or 0}
    rows = await kaipanla_plate.fetch_realtime_rank()
    if len(rows) < MIN_ROWS:
        raise ValueError(f"incomplete realtime plate rank: {len(rows)}")
    fp = fingerprint(rows)
    prev_fp = meta.get("fingerprint")
    published = await _publish(day, rows, "partial_preview")
    stable = bool(prev_fp and prev_fp == fp)
    after_1600 = now.astimezone(TZ).time() >= dtime(16, 0)
    if stable or after_1600:
        store.plate_rank_day_meta_save(
            PLATE_TYPE,
            day,
            status="final",
            source=SOURCE,
            collected_at=published["collected_at"],
            row_count=published["rows"],
            fingerprint=fp,
        )
        published["status"] = "final"
        published["finalize_reason"] = "stable" if stable else "after_1600"
    else:
        published["finalize_reason"] = "waiting_stable"
    return published


def history_gaps(end: str, lookback: int = 20) -> list[str]:
    """需要历史补齐的过去交易日。不含 end 当天。"""
    calendar = (store.kv_get("collector_calendar_v1", {}) or {}).get("days") or []
    days = [str(d)[:10] if "-" in str(d) else f"{str(d)[:4]}-{str(d)[4:6]}-{str(d)[6:8]}" for d in calendar]
    days = [d for d in days if d < end][-lookback:]
    if not days:
        # 没有日历时，用已有快照往前推不够；返回空，由调用方用工作日探测
        return []
    gaps = []
    for day in days:
        meta = _meta_for(day)
        count = int(meta.get("row_count") or store.plate_rank_day_count(PLATE_TYPE, day))
        if meta.get("status") == "final" and count >= MIN_ROWS:
            continue
        gaps.append(day)
    return gaps


def history_gaps_weekdays(end: str, lookback: int = 20) -> list[str]:
    """日历缺失时的兜底：只扫工作日，不含当天。

    跨度按 lookback 放大（约 3×工作日），避免旧逻辑硬顶 ~40 自然日导致更早缺口永远扫不到。
    """
    end_d = datetime.fromisoformat(end).date()
    out = []
    d = end_d - timedelta(days=1)
    span = max(lookback * 3 + 10, 40)
    while len(out) < lookback and (end_d - d).days < span:
        if d.weekday() < 5:
            day = d.isoformat()
            meta = _meta_for(day)
            count = int(meta.get("row_count") or store.plate_rank_day_count(PLATE_TYPE, day))
            if count < MIN_ROWS or meta.get("status") != "final":
                out.append(day)
        d -= timedelta(days=1)
    return out


async def backfill_history(end: str | None = None, lookback: int = 20) -> dict:
    """历史只补缺。已 final 且行数够的不重拉。"""
    end = end or datetime.now(TZ).date().isoformat()
    gaps = history_gaps(end, lookback) or history_gaps_weekdays(end, lookback)
    saved, errors = [], []
    for day in gaps:
        try:
            rows = await kaipanla_plate.fetch_history_rank(day)
            result = await _publish(day, rows, "final")
            saved.append({"date": day, "rows": result["rows"]})
        except Exception as exc:  # noqa: BLE001 - 单日失败不中断其余
            errors.append({"date": day, "error": type(exc).__name__})
    return {"end": end, "saved": saved, "errors": errors, "gaps": gaps}
