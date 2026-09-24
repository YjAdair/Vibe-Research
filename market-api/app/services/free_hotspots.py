"""独立免费热点数据服务。

本模块只读已发布的本地数据；``boards.members`` 仅由显式 worker
``collect_members``，或 embedded 模式下当前请求的 singleflight 刷新调用，用于把
完整结果写入 KV。external/off API 不回源。这里不读取 legacy 题材/板块文件，也不
把东财 BK 代码转换成 801 代码。
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.config import settings
from app.core.store import store
from app.core.cache import cache
from app.services import boards
from app.services.popular import INDEPENDENT_SOURCES

TZ = ZoneInfo("Asia/Shanghai")
FORMULA_VERSION = "free_hotspots_v1"
MEMBER_PREFIX = "free_hotspots:members:"
INTERVALS = ["20-40", "40-60", "60-80", "80-100", "100+"]
_member_locks: dict[str, asyncio.Lock] = {}


def _date(value: str) -> str:
    raw = str(value or "").replace("-", "")
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError("Invalid trade date")
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"


def _code(value) -> str:
    raw = str(value or "").strip()
    return raw.zfill(6) if raw.isdigit() else raw


def _board_type(plate_type: int) -> int:
    # 14/15 are Eastmoney-native namespaces. Target 17/18 are deliberately
    # not translated into a provider namespace.
    if plate_type == 15:
        return 3
    if plate_type == 14:
        return 2
    raise ValueError("Unsupported plate_type")


def _taxonomy(plate_type: int) -> str:
    return "eastmoney_concept" if plate_type == 15 else "eastmoney_industry"


def unsupported_taxonomy(plate_type: int) -> dict:
    return {
        "status": "unsupported_taxonomy",
        "taxonomy": f"target_{'subtopic' if int(plate_type) == 18 else 'topic'}",
        "target_taxonomy_id": int(plate_type),
        "source": None,
        "data": [],
        "reason": "目标分类与东财原生14行业/15概念的等价关系未验证",
    }


def _published_days(end: str, count: int) -> list[str]:
    if count < 1:
        return []
    calendar = store.kv_get("collector_calendar_v1", {}) or {}
    if isinstance(calendar, dict) and calendar.get("complete") is False:
        return []
    raw_days = calendar.get("days", []) if isinstance(calendar, dict) else []
    days_set = set()
    for raw_day in raw_days:
        if not str(raw_day).replace("-", "").isdigit():
            continue
        try:
            days_set.add(_date(raw_day))
        except ValueError:
            continue
    days = sorted(days_set, reverse=True)
    return [d for d in days if d <= end][:count]


def _range_rows(board_type: int, start: str, end: str) -> dict[tuple[str, str], dict]:
    """Merge history and end-of-day snapshots by (date, native BK code)."""
    snapshots, runs = store.board_snapshot_range(board_type, start, end)
    history = store.board_history_range(board_type, start, end)
    merged: dict[tuple[str, str], dict] = {}
    run_by_day = {
        _date(run.get("trade_date")): run
        for run in runs if run.get("trade_date")
    }
    for row in history:
        day = row.get("date1") or row.get("trade_date")
        code = row.get("plate_code")
        if day and code:
            merged[(_date(day), str(code))] = {**row, "_data_kind": row.get("data_kind") or "historical_daily"}
    # A snapshot is the observed value for the same day and therefore replaces
    # the historical copy; it must never be counted as a second observation.
    for row in snapshots:
        day = row.get("date1") or row.get("trade_date")
        code = row.get("plate_code")
        if day and code:
            key = (_date(day), str(code))
            old = merged.get(key, {})
            merged[key] = {
                **old,
                **{k: v for k, v in row.items() if v is not None or k not in old},
                "_data_kind": "observed_snapshot",
                "_run_meta": run_by_day.get(key[0], {}),
            }
    return merged


def _metric(row: dict, name: str):
    if name == "rate":
        return row.get("rate") if row.get("rate") is not None else row.get("pct")
    return (row.get("main_net_inflow") if row.get("main_net_inflow") is not None
            else row.get("money_leader"))


def _sum_or_missing(values: list) -> tuple[float | None, int]:
    valid = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(float(v))]
    return (sum(valid), len(valid)) if len(valid) == len(values) else (None, len(valid))


def _row_meta(row: dict) -> tuple[object, object]:
    run = row.get("_run_meta") or {}
    source_as_of = (row.get("source_as_of") or row.get("source_timestamp")
                    or run.get("source_as_of"))
    collected_at = row.get("collected_at") or run.get("collected_at")
    return source_as_of, collected_at


def _source_datetime(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            stamp = float(value)
            if stamp > 10**12:
                stamp /= 1000
            return datetime.fromtimestamp(stamp, TZ)
        raw = str(value).strip()
        if raw.isdigit() and len(raw) == 14:
            return datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=TZ)
        if raw.isdigit() and len(raw) == 8:
            return datetime.strptime(raw, "%Y%m%d").replace(tzinfo=TZ)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return (parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _freshness(source_as_of, *, day: str | None = None, realtime: bool = False) -> dict:
    """返回可审计的新鲜度，不把未知时间默认为新鲜。"""
    checked_at = datetime.now(TZ).isoformat()
    parsed = _source_datetime(source_as_of)
    if parsed is None:
        return {"status": "unknown", "source_as_of": source_as_of, "checked_at": checked_at}
    age = (datetime.now(TZ) - parsed).total_seconds()
    if day and parsed.date().isoformat() != day:
        status = "stale_date"
    elif realtime and (age < -1 or age > 120):
        # A verified 15:00-ish close can be displayed after the session while
        # remaining explicitly session-closed; it is never treated as fresh.
        if age >= 0 and day and parsed.date().isoformat() == day and datetime.now(TZ).time().hour >= 15 and parsed.time().hour >= 15:
            status = "session_closed"
        elif age >= 0 and day and parsed.date().isoformat() == day and time(11, 30) <= datetime.now(TZ).time() < time(13) and parsed.time() >= time(11, 30):
            status = "session_paused"
        else:
            status = "stale"
    else:
        status = "fresh" if realtime else "published"
    return {"status": status, "source_as_of": source_as_of, "age_seconds": round(max(age, 0), 3),
            "max_age_seconds": 120 if realtime else None, "checked_at": checked_at}


def _freshness_summary(rows: list[dict], *, realtime: bool = False) -> dict:
    states = {str((row.get("freshness") or {}).get("status") or "unknown") for row in rows}
    if not states:
        status = "unknown"
    elif len(states) == 1:
        status = states.pop()
    elif states <= {"fresh", "unknown"}:
        status = "partial"
    else:
        status = "stale"
    return {"status": status, "checked_at": datetime.now(TZ).isoformat(),
            "max_age_seconds": 120 if realtime else None}


async def rank_days(date2, n_days, n_type, limit=20, plate_type=17) -> list[dict]:
    """按发布交易日历聚合东财原生板块快照/历史。

    n_type=1 是每日涨跌幅简单求和，``formula_note`` 明确它不是复合收益；
    n_type=3 是主力净流入求和；n_type=9 没有可验证的免费复原口径，返回空。
    聚合缺少任一日的指标时返回 ``null``，绝不把缺值当作零。
    """
    if int(plate_type) in (17, 18):
        return unsupported_taxonomy(int(plate_type))
    if int(n_type) == 9:
        return []
    n_days = int(n_days)
    if n_days < 1:
        return []
    if int(n_type) not in (1, 3):
        return []
    end = _date(date2)
    dates = _published_days(end, n_days)
    if len(dates) != n_days:
        return []
    board_type = _board_type(int(plate_type))
    merged = _range_rows(board_type, dates[-1], dates[0])
    by_code: dict[str, dict[str, dict]] = {}
    for (day, code), row in merged.items():
        if day in dates:
            by_code.setdefault(code, {})[day] = row
    metric_name = "rate" if int(n_type) == 1 else "flow"
    formula_note = "sum_daily_pct_non_compounded" if int(n_type) == 1 else "sum_main_net_inflow"
    result = []
    for code, daily in by_code.items():
        from app.services.topic import is_statistical_board
        current = next((daily[d] for d in dates if d in daily), {})
        if is_statistical_board(current.get('plate_name') or current.get('name') or ''):
            continue
        rate_total, rate_days = _sum_or_missing([_metric(daily.get(day, {}), "rate") for day in dates])
        flow_total, flow_days = _sum_or_missing([_metric(daily.get(day, {}), "flow") for day in dates])
        selected_complete = rate_days == n_days if int(n_type) == 1 else flow_days == n_days
        # A null sorting value is not a rankable result. The second aggregate is
        # still calculated independently and may remain null.
        if not selected_complete:
            continue
        latest = next((daily[d] for d in dates if d in daily), {})
        source_as_of, collected_at = _row_meta(latest)
        data_kind = latest.get("_data_kind") or ("observed_snapshot" if latest.get("date1") else "historical_daily")
        result.append({
            "plate_code": code,
            "name": latest.get("plate_name") or latest.get("name") or code,
            "plate_name": latest.get("plate_name") or latest.get("name") or code,
            "sum_rate": rate_total,
            "sum_score": None,
            "sum_leader_money": flow_total,
            "days": n_days,
            "last_day": max(daily) if daily else None,
            "source": "eastmoney_board_snapshots+board_daily_history",
            "taxonomy": _taxonomy(int(plate_type)),
            "formula_version": FORMULA_VERSION,
            "formula_note": formula_note,
            "data_status": "complete" if rate_days == n_days and flow_days == n_days else "partial",
            "data_kind": data_kind,
            "source_as_of": source_as_of,
            "collected_at": collected_at,
            "freshness": _freshness(source_as_of),
        })
    key = "sum_rate" if int(n_type) == 1 else "sum_leader_money"
    result.sort(key=lambda r: (r[key] is None, -(r[key] or 0), r["plate_code"]))
    return result[:max(0, int(limit))]


async def trend(code, start, end) -> list[dict]:
    """读东财概念/行业原生 BK 的板块历史，按日期合并同日快照。"""
    code = str(code or "").strip()
    start, end = _date(start), _date(end)
    if not code or start > end:
        return []
    all_rows: dict[str, dict] = {}
    for board_type in (3, 2):
        rows = _range_rows(board_type, start, end)
        for (day, row_code), row in rows.items():
            if row_code == code:
                # If a code were ever present in both native namespaces, the
                # concept namespace wins consistently; each day still appears once.
                all_rows.setdefault(day, row)
    out = []
    for day in sorted(all_rows):
        row = all_rows[day]
        out.append({
            "date1": day,
            "rate": _metric(row, "rate"),
            "score": None,
            "trade_money": row.get("trade_money") if row.get("trade_money") is not None else row.get("amount"),
            "money_leader": _metric(row, "flow"),
            "source": row.get("source") or "eastmoney_board_snapshots+board_daily_history",
            "source_as_of": _row_meta(row)[0],
            "collected_at": _row_meta(row)[1],
            "data_kind": row.get("_data_kind") or "historical_daily",
        })
    return out


def _member_key(code: str, suffix: str) -> str:
    return f"{MEMBER_PREFIX}{code}:{suffix}"


def _member_record(code: str, day: str | None = None, *, at_open=False) -> dict | None:
    code = str(code or "").strip()
    index = store.kv_get(_member_key(code, "index"), []) or []
    records = [store.kv_get(d if d.startswith(MEMBER_PREFIX) else _member_key(code, d)) for d in index]
    if not at_open:
        latest = store.kv_get(_member_key(code, "latest"))
        if latest:
            records.append(latest)
    from app.services.ml_r1 import known_before
    records = [r for r in records if r and (day is None or str(r.get('as_of') or '') <= day)
               and (not at_open or (r.get('source') == 'eastmoney' and r.get('status') == 'ok'
                    and known_before(r.get('collected_at'), day + 'T09:30:00')))]
    return max(records, key=lambda r: str(r.get('collected_at') or r.get('as_of') or '')) if records else None


def _public_member(record: dict | None) -> dict:
    if not record:
        return {"stocks": [], "as_of": None, "effective_date": None, "source_as_of": None,
                "status": "missing", "source": "eastmoney", "freshness": {"status": "unknown"}}
    return {k: record.get(k) for k in ("stocks", "as_of", "effective_date", "source_as_of", "status", "source", "freshness", "collected_at")}


async def _collect_members_locked(code: str) -> dict:
    """在调用方持有 code lock 时完成一次完整成员采集。"""
    failure_key = 'free_hotspots:member_failures:' + code
    failures = store.kv_get(failure_key, {}) or {}
    if failures.get('attempts', 0) >= 3:
        raise ValueError('Member source attempt limit reached')
    try:
        data = await boards.members(code)
        rows = list(data.get('stocks') or [])
        expected = int(data.get('total') if data.get('total') is not None else -1)
        identities = [_code(r.get('code') or r.get('stock_code') or r.get('symbol_code')) for r in rows]
        if expected <= 0 or len(rows) != expected or any(not c for c in identities) or len(set(identities)) != expected:
            raise ValueError('Incomplete or duplicate member batch')
    except Exception as exc:
        store.kv_set(failure_key, {'attempts': failures.get('attempts', 0) + 1,
                                  'last_error': type(exc).__name__, 'collected_at': datetime.now(TZ).isoformat(),
                                  'next_step': '达到3次后停止，核实来源恢复证据后人工处理'})
        raise
    raw_rows = list(data.get("stocks") or [])
    total = int(data.get("total") if data.get("total") is not None else -1)
    codes = [_code(r.get("code") or r.get("stock_code") or r.get("symbol_code")) for r in raw_rows]
    collected_at = datetime.now(TZ).isoformat()
    # This is the wall-clock collection date. Provider quote timestamps are
    # metadata only and are never used as the membership effective date.
    as_of = collected_at[:10]
    record = {
        "stocks": codes,
        "quotes": {c: {**row, "code": c} for c, row in zip(codes, raw_rows)},
        "as_of": as_of,
        "effective_date": as_of,
        "source_as_of": data.get("source_as_of"),
        "status": "ok",
        "source": data.get("source") or "eastmoney",
        "total": total,
        "collected_at": collected_at,
        "quotes_collected_at": collected_at,
        "freshness": {"status": "fresh", "checked_at": collected_at,
                       "max_age_seconds": 120, "source_as_of": data.get("source_as_of")},
    }
    store.kv_append_snapshot(MEMBER_PREFIX + code, record)
    store.kv_set(_member_key(code, "latest"), record)
    return _public_member(record)


async def members_snapshot(code) -> dict:
    """读取当前成员快照；同一 code 的缺缓存请求 singleflight。

    external/off 是 API 只读模式；embedded 才允许缓存缺失或 quote 超过 60 秒
    时按需采集。成员的 effective_date 仍是实际采集日，不是 quote 时间。
    """
    code = str(code or "").strip()
    if not code:
        return _public_member(None)
    async with _member_locks.setdefault(code, asyncio.Lock()):
        hit = _member_record(code)
        today = datetime.now(TZ).date().isoformat()
        refreshed = _source_datetime((hit or {}).get("quotes_collected_at"))
        age = ((datetime.now(TZ) - refreshed).total_seconds() if refreshed else None)
        if hit and (settings.collector_mode != "embedded" or
                    (hit.get("as_of") == today and age is not None and 0 <= age <= 120)):
            public = _public_member(hit)
            public["freshness"] = _freshness((hit or {}).get("quotes_collected_at"), realtime=True)
            return public
        if settings.collector_mode != "embedded":
            public = _public_member(hit)
            public["freshness"] = _freshness((hit or {}).get("quotes_collected_at"), realtime=True)
            return public
        return await _collect_members_locked(code)


async def collect_members(code) -> dict:
    """显式 worker：完整采集 boards.members，并按实际采集日写 KV。"""
    code = str(code or "").strip()
    if not code:
        raise ValueError("Missing board code")
    async with _member_locks.setdefault(code, asyncio.Lock()):
        return await _collect_members_locked(code)


def membership(code, day, *, at_open=False) -> dict:
    """返回不晚于 day 的成员快照；绝不读取 legacy 文件。"""
    return _public_member(_member_record(str(code or "").strip(), _date(day), at_open=at_open))


def _valid(value) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _quote_meta(day: str) -> dict:
    run = store.daily_close_run(day) or {}
    return {"source_as_of": run.get("source_as_of"), "collected_at": run.get("collected_at"), "run": run}


def _current_market_snapshot(day: str) -> dict | None:
    cache_key = f"free_market_quotes:{store.path}:{day}"
    hit = cache.get(cache_key)
    if hit is not None:
        return hit
    snap = store.kv_get("market_quotes_current_v1", {}) or {}
    if not isinstance(snap, dict) or snap.get("complete") is not True or snap.get("date") != day:
        return None
    stocks = snap.get("stocks")
    if not isinstance(stocks, list):
        return None
    rows = [row for row in stocks if isinstance(row, dict) and _code(row.get("code"))]
    if len(rows) != len(stocks) or not rows:
        return None
    result = {**snap, "stocks_by_code": {_code(row.get("code")): row for row in rows}}
    cache.set(cache_key, result, 2)
    return result


def _quote_rows(codes: list[str], day: str, record: dict | None) -> list[dict]:
    today = datetime.now(TZ).date().isoformat()
    shared = _current_market_snapshot(day) if day == today else None
    is_today = day == today
    if shared:
        current, current_quotes = shared, shared.get("stocks_by_code") or {}
    elif is_today:
        current = record if record and record.get("as_of") == today else {}
        current_quotes = ((record or {}).get("quotes") or {}) if current else {}
    else:
        current, current_quotes = None, {}
    closes = store.daily_close_by_codes(day, codes) if current is None else {}
    historical = store.kline_by_codes(day, codes) if current is None else {}
    daily_meta = _quote_meta(day)
    rows = []
    for code in codes:
        price_evidence = {}
        raw = current_quotes.get(code) if current is not None else None
        q = raw or closes.get(code) or {}
        if current is not None and not raw:
            # A same-day member without a current quote remains missing; no old
            # close is substituted. This keeps current quote coverage honest.
            q = {}
        if current is not None:
            close = q.get("price")
            prev = q.get("prev_close")
            source_as_of = q.get("source_timestamp")
            freshness = _freshness(source_as_of, day=day, realtime=True)
            source_status = freshness.get("status")
            source_valid = source_status in ("fresh", "session_closed", "session_paused")
            if not source_valid:
                close, pct, amount = None, None, None
            elif _valid(close) and _valid(prev):
                pct = (float(close) / float(prev) - 1) * 100
            elif isinstance(q.get("pct"), (int, float)) and math.isfinite(float(q["pct"])):
                pct = float(q["pct"])
            else:
                pct = None
            name = q.get("name") or code
            if source_valid:
                amount = q.get("amount")
            quote_source = "market_quotes_current_v1" if shared else "eastmoney_board_members"
            if not source_valid:
                status = "stale_quote" if freshness.get("status") in ("stale", "stale_date") else "missing_timestamp"
            elif source_status in ("session_closed", "session_paused") and (_valid(close) or pct is not None):
                status = source_status
            elif _valid(close) and pct is not None:
                status = "ok"
            elif not _valid(close) and pct is not None:
                status = "missing_price"
            elif _valid(close):
                status = "missing_pct"
            else:
                status = "missing_quote"
        else:
            close, prev = q.get("close"), q.get("prev_close")
            # 旧daily_close曾将上一根裸收盘补作官方参考；没有新版采集证据不再输出该比值。
            reference_verified = daily_meta['run'].get('all_priced_rows_eod_verified') is True
            pct = (float(close) / float(prev) - 1) * 100 if reference_verified and _valid(close) and _valid(prev) else None
            name = ((record or {}).get('quotes',{}).get(code) or {}).get('name') or q.get("stock_name") or code
            amount = q.get("amount")
            source_as_of = q.get("source_as_of") or daily_meta.get("source_as_of")
            quote_source = "published_daily_close"
            status = "ok" if pct is not None else ("missing_reference_evidence" if _valid(close) and not reference_verified else "missing_prev_close" if _valid(close) else "missing_quote")
            freshness = _freshness(source_as_of, day=day, realtime=False)
            from app.services.ml_history import complete_daily_bar
            bar = historical.get(code)
            if complete_daily_bar(bar,day):
                meta = bar['input_meta']
                proof = meta.get('historical_daily') or {}
                close,prev,amount = bar.get('close'),bar.get('prev_close'),bar.get('amount')
                pct = (float(close)/float(prev)-1)*100 if meta.get('official_reference_verified') is True and _valid(close) and _valid(prev) else None
                source_as_of = meta.get('source_as_of')
                quote_source = bar.get('source')
                status = 'historical_close' if proof else 'session_closed'
                price_evidence = {'kind':'historical_daily' if proof else 'verified_eod',
                                  'known_at':proof.get('known_at'),'original_published_at':proof.get('original_published_at'),
                                  'raw_snapshot_id':proof.get('raw_snapshot_id'),'backtest_point_in_time_verified':False}
                freshness = {'status':'historical_complete','source_as_of':source_as_of,'checked_at':datetime.now(TZ).isoformat()}
        rows.append({
            "stock_code": code,
            "stock_name": name,
            "px_change_rate": pct,
            "last_px": float(close) if _valid(close) else None,
            "amount": amount,
            "trade_money": amount,
            "source_as_of": source_as_of,
            "quote_date": day,
            "quote_source": quote_source,
            "data_status": status,
            "freshness": freshness,
            "price_evidence": price_evidence,
        })
    rows.sort(key=lambda r: (r["px_change_rate"] is None, -(r["px_change_rate"] or 0), r["stock_code"]))
    return rows


def _paged(rows: list[dict], page: int, limit: int) -> dict:
    page, limit = max(1, int(page)), max(1, int(limit))
    start = (page - 1) * limit
    return {"list": rows[start:start + limit], "total": len(rows), "page": page, "limit": limit}


async def stock_rates(code, day, page, limit, *, at_open=False, member_record=None) -> dict:
    day = _date(day)
    code = str(code or "").strip()
    record = member_record if member_record is not None else _member_record(code, day, at_open=at_open)
    if not at_open and day == datetime.now(TZ).date().isoformat():
        # A current request may fill a missing/stale current snapshot. Historical
        # requests never await a collector and cannot leak future members.
        if not record or not _current_market_snapshot(day):
            await members_snapshot(code)
            record = _member_record(code, day)
        if not record:
            return {**_paged([], page, limit), "meta": {"date": day, "status": "missing_current_members",
                    "freshness": _freshness(None, day=day, realtime=True)}}
    if not record:
        return {**_paged([], page, limit), "meta": {"date": day, "status": "missing_preopen_members" if at_open else "missing_members",
                "freshness": _freshness(None, day=day, realtime=False)}}
    rows = _quote_rows(list(record.get("stocks") or []), day, record)
    valid = sum(r["px_change_rate"] is not None and r["last_px"] is not None for r in rows)
    status = "ok" if valid == len(rows) else "partial" if valid else "missing_quotes"
    return {**_paged(rows, page, limit), "meta": {"date": day, "status": status, "coverage": {"valid_quotes": valid, "expected": len(rows)}, "membership_as_of": record.get("as_of"), "membership_known_at": (record.get("known_at") or record.get("collected_at")), "membership_cutoff": day + "T09:30:00+08:00" if at_open else None, "source": (record.get("source") or "eastmoney_board_members") + "+published_quotes",
            "freshness": _freshness_summary(rows, realtime=day == datetime.now(TZ).date().isoformat())}}


async def popular_rank(code, day, page, limit, with_pct=1, *, at_open=False, member_record=None) -> dict:
    day = _date(day)
    record = member_record if member_record is not None else _member_record(str(code or "").strip(), day, at_open=at_open)
    snap = store.popular_get(day)
    meta = {"date": day, "status": "missing_preopen_members" if at_open and not record else "missing", "membership_as_of": record.get("as_of") if record else None, "membership_known_at": (record or {}).get("collected_at"), "membership_cutoff": day + "T09:30:00+08:00" if at_open else None}
    if not record or not snap or snap.get("date") != day or snap.get("complete") is not True or snap.get("source") not in INDEPENDENT_SOURCES:
        return {**_paged([], page, limit), "meta": meta}
    members = set(record.get("stocks") or [])
    hits = []
    for item in snap.get("items") or []:
        item_code = _code(item.get("symbol_code"))
        if item_code in members:
            hits.append((item_code, item))
    hits.sort(key=lambda x: (x[1].get("rank") is None, x[1].get("rank") if x[1].get("rank") is not None else 0, x[0]))
    quote_codes = [c for c, _ in hits]
    quotes = {r["stock_code"]: r for r in _quote_rows(quote_codes, day, record)} if with_pct else {}
    rows = []
    for item_code, item in hits:
        row = {"stock_code": item_code, "stock_name": item.get("symbol_name") or item_code,
               "rank": item.get("rank"), "rank_diff_provider": item.get("rank_diff"),
               "rank_diff": item.get("rank_diff") if snap.get("rank_diff_basis") == "previous_trading_day_same_time" else None,
               "attention": item.get("heat") if item.get("heat") is not None else item.get("attention")}
        if with_pct:
            row.update({k: quotes[item_code][k] for k in ("px_change_rate", "last_px", "amount", "trade_money", "source_as_of", "quote_date", "quote_source", "data_status")})
        rows.append(row)
    meta.update(status="ok", source=snap.get("source") or "published_popular", popular_date=snap.get("date"),
                freshness=_freshness_summary(list(quotes.values()), realtime=day == datetime.now(TZ).date().isoformat()) if with_pct else _freshness(snap.get("collected_at")))
    pool_size = len(snap.get("items") or [])
    meta.update(rank_pool_size=pool_size, rank_scope="vendor_published_pool", ranking_complete_for_board=False,
                popularity_collected_at=snap.get("collected_at"), popularity_as_of=snap.get("source_as_of"),
                rank_change_status="verified" if snap.get("rank_diff_basis") == "previous_trading_day_same_time" else "missing_same_time_baseline",
                note=f"人气仅为供应商公开榜单{pool_size}名与板块交集，不代表板块全体TopN；名次变化仅在同时间基准已验证时显示。")
    return {**_paged(rows, page, limit), "meta": meta}


def _empty_pct(day: str, days: int, status: str = "missing") -> dict:
    return {"intervals": INTERVALS, "stocks": {iv: [] for iv in INTERVALS},
            "meta": {"date": day, "days": days, "status": status, "return_basis": "unadjusted_price_return", "strategy_backtest": False}}


def _interval(pct: float) -> str | None:
    if pct < 20:
        return None
    if pct < 40:
        return "20-40"
    if pct < 60:
        return "40-60"
    if pct < 80:
        return "60-80"
    if pct < 100:
        return "80-100"
    return "100+"


async def pct_batch(code, dates, days) -> dict:
    days = int(days)
    result = {}
    for raw_day in dates or []:
        day = _date(raw_day)
        calendar = _published_days(day, days + 1)
        if len(calendar) != days + 1 or calendar[0] != day:
            result[day] = _empty_pct(day, days)
            continue
        base_day = calendar[-1]
        record = _member_record(str(code or "").strip(), day)
        if not record:
            result[day] = _empty_pct(day, days, "missing_members")
            continue
        rows = store.daily_close_range(day, days + 1)
        by_date = {}
        for row in rows:
            by_date.setdefault(row.get("trade_date"), {})[_code(row.get("stock_code"))] = row
        terminal = by_date.get(day, {})
        base = by_date.get(base_day, {})
        out = {iv: [] for iv in INTERVALS}
        covered = 0
        for stock_code in record.get("stocks") or []:
            e, b = terminal.get(stock_code), base.get(stock_code)
            if not e or not b or not _valid(e.get("close")) or not _valid(b.get("close")):
                continue
            covered += 1
            pct = (float(e["close"]) / float(b["close"]) - 1) * 100
            iv = _interval(pct)
            if iv:
                out[iv].append({"stock_code": stock_code, "stock_name": e.get("stock_name") or b.get("stock_name") or stock_code, "cum_pct": round(pct, 2)})
        for values in out.values():
            values.sort(key=lambda row: (-row["cum_pct"], row["stock_code"]))
        result[day] = {"intervals": INTERVALS, "stocks": out, "meta": {
            "date": day, "base_date": base_day, "terminal_date": day, "days": days,
            "quote_coverage": covered, "member_total": len(record.get("stocks") or []),
            "status": "complete" if covered and covered == len(record.get("stocks") or []) else "partial" if covered else "missing_quotes",
            "membership_as_of": record.get("as_of"),
            "return_basis": "unadjusted_price_return", "strategy_backtest": False,
            "source": "published_daily_close",
            "freshness": _freshness((store.daily_close_run(day) or {}).get("collected_at")),
        }}
    return result


async def kline(code, n=250, end: str | None = None) -> dict:
    code = str(code or "").strip()
    n = max(1, int(n))
    merged: dict[str, dict] = {}
    for board_type in (3, 2):
        for row in store.board_history_range(board_type, "0001-01-01", end or "9999-12-31"):
            day = row.get("date1") or row.get("trade_date")
            if day and row.get("plate_code") == code:
                merged.setdefault(_date(day), row)
    bars = []
    previous_close = None
    for day in sorted(merged):
        row = merged[day]
        close = row.get("price") if row.get("price") is not None else row.get("close")
        values = [row.get("open"), close, row.get("high"), row.get("low")]
        if any(v is None for v in values):
            continue
        prev = row.get("prev_close") if row.get("prev_close") is not None else previous_close
        bars.append((day, values, prev, row))
        previous_close = close
    bars = bars[-n:]
    latest_row = bars[-1][3] if bars else {}
    latest_source_as_of = latest_row.get("source_as_of")
    return {
        "x": [day.replace("-", "") for day, _, _, _ in bars],
        "y": [values + [prev] for _, values, prev, _ in bars],
        "vol": [row.get("volume") for _, _, _, row in bars],
        # Compatibility field: historical board contract uses turnover for CNY
        # amount. The provider's raw turnover ratio is retained separately.
        "amount": [row.get("amount") for _, _, _, row in bars] or None,
        "turnover": [row.get("amount") for _, _, _, row in bars] or None,
        "turnover_ratio": [row.get("turnover") for _, _, _, row in bars] or None,
        "turnover_semantics": "amount_cny",
        "amount_unit": "CNY",
        "source": sorted({str(row.get("source") or "unknown") for _, _, _, row in bars}),
        "taxonomy": "eastmoney_boards",
        "adjustment": "none",
        "as_of_date": bars[-1][0].replace("-", "") if bars else None,
        "frequency": "daily",
        "series_kind": "ohlc",
        "requested_end": end,
        "freshness": _freshness(latest_source_as_of),
    }
