"""市场数据服务：交易日、K线、分时、实时行情、板块热度。"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

from app.core.cache import cache
from app.core.store import store
from app.datasources import eastmoney, sina, tencent
from app.datasources.codes import normalize_code, market_of, to_tencent_symbol
from app.services.topic import is_statistical_board


# 常见指数映射：同花顺/常见代码 -> 行情 symbol
INDEX_SYMBOLS = {
    "883957": "sh000001",  # 上证指数
    "000001": "sh000001",
    "sh000001": "sh000001",
    "399001": "sz399001",  # 深证成指
    "sz399001": "sz399001",
    "399006": "sz399006",  # 创业板指
    "sz399006": "sz399006",
    "000300": "sh000300",  # 沪深300
    "sh000300": "sh000300",
    "000905": "sh000905",  # 中证500
    "sh000905": "sh000905",
    "000852": "sh000852",  # 中证1000
    "sh000852": "sh000852",
    "000016": "sh000016",  # 上证50
    "sh000016": "sh000016",
}


def resolve_symbol(code: str) -> str:
    """把股票/指数代码解析为行情 symbol。"""
    c = normalize_code(code)
    if c in INDEX_SYMBOLS:
        return INDEX_SYMBOLS[c]
    return to_tencent_symbol(c)


async def trade_days(n: int = 60) -> list[str]:
    """近 n 个交易日。默认读采集器发布的日历，避免页面打开时打上游。"""
    from app.core.store import store
    published = store.kv_get('collector_calendar_v1', {}) or {}
    days = [str(d).replace('-', '') for d in published.get('days') or []]
    if days:
        return days[-n:]
    key = f"trade_days:{n}"
    hit = cache.get(key)
    if hit:
        return hit
    from app.config import settings
    if settings.collector_mode != 'embedded':
        return []
    try:
        data = await tencent.kline_day_by_symbol("sh000001", n)
    except Exception:
        data = await sina.kline_day("sh000001", n)
    days = [d for d in data["x"]]
    cache.set(key, days, 24 * 3600)
    return days


async def kline(code: str, n: int = 250) -> dict:
    c = str(code or "").strip()
    if is_plate_code(c):
        return await plate_kline_contract(c, "main", n)
    key = f"kline:{code}:{n}"
    hit = cache.get(key)
    if hit:
        return hit
    try:
        data = await tencent.kline_day_by_symbol(resolve_symbol(code), n)
    except Exception:
        data = await sina.kline_day(code, n)
    cache.set(key, data, 300)
    return data


PLATE_CODE_RE = re.compile(r"^(801|803|804|881|883|885|886)\d{3}$")


def is_plate_code(code: str) -> bool:
    """同花顺系板块/指数代码段：801/803/804(hexin 板块)、881/885/886(d10jqka)、883(特殊板)。"""
    return bool(PLATE_CODE_RE.match(str(code or "").strip()))


def _items_to_contract(items: list[dict]) -> dict:
    """plate_kline() 的 p_open/p_close 条目 → StockKlineDay 契约。"""
    x, y, vol, turnover = [], [], [], []
    for it in items:
        day = str(it.get("date") or "").replace("-", "")
        if len(day) != 8:
            continue
        x.append(day)
        y.append([it.get("p_open"), it.get("p_close"), it.get("p_high"), it.get("p_low"), it.get("p_prev_close")])
        vol.append(int(float(it.get("volume") or 0)))
        turnover.append(it.get("amount"))
    return {"x": x, "y": y, "vol": vol, "turnover": turnover or None}


def _bars_to_contract(bars: list[dict]) -> dict:
    """ths_board_daily 真实 bar({date,open,high,low,close,volume,amount}) → StockKlineDay 契约。

    首根无昨收填 None，前端 splitData 自带回退链（y[4] ?? 前一根收盘 ?? 当日收盘）。
    """
    x, y, vol, turnover = [], [], [], []
    prev_close = None
    for b in bars:
        day = str(b.get("date") or "").replace("-", "")
        if len(day) != 8:
            continue
        x.append(day)
        y.append([b.get("open"), b.get("close"), b.get("high"), b.get("low"), prev_close])
        vol.append(int(float(b.get("volume") or 0)))
        turnover.append(b.get("amount"))
        prev_close = b.get("close")
    return {"x": x, "y": y, "vol": vol, "turnover": turnover or None}


def _slice_contract(data: dict, n: int) -> dict:
    x = data.get("x") or []
    if n and len(x) > n:
        return {k: (v[-n:] if isinstance(v, list) else v) for k, v in data.items()}
    return data


def plate_ew_daily_nav(code: str, n: int = 250, date_end: str | None = None) -> dict:
    """801/803 免费日净值：成分等权日收益链式净值，明示 series_kind=daily_nav，不伪造 OHLC。

    点位成分取 plate_members_full(code, day)；缺成分或缺双边收盘的日子跳过（不填 0）。
    起点净值 1000；y 为标量序列（与 ml_r1 daily_nav 同形）。
    """
    from app.services import plate_flow

    empty = {
        "x": [], "y": [], "vol": [], "turnover": None, "amount": None,
        "series_kind": "daily_nav", "status": "missing",
        "reason": "missing_member_closes", "source": "ew_member_daily_close",
        "frequency": "daily", "as_of_date": None,
    }
    code = str(code or "").strip()
    n = max(1, int(n or 250))
    end = str(date_end or "").replace("/", "-")[:10]
    if end and len(end) == 8 and end.isdigit():
        end = f"{end[:4]}-{end[4:6]}-{end[6:8]}"
    if not end:
        with store._conn() as conn:
            row = conn.execute("SELECT MAX(trade_date) FROM daily_close").fetchone()
        end = (row[0] if row else None) or ""
    if not code or not end:
        return empty

    # 多取若干交易日：等权收益需要前收，且缺双边收盘的日子会被跳过
    rows = store.daily_close_range(end, n + 5)
    by_date: dict[str, dict[str, float]] = {}
    for r in rows:
        close = r.get("close")
        if not isinstance(close, (int, float)):
            continue
        by_date.setdefault(str(r["trade_date"]), {})[str(r["stock_code"])] = float(close)
    dates = sorted(by_date)
    if len(dates) < 2:
        return empty

    x, y, coverage = [], [], []
    nav = 1000.0
    for i in range(1, len(dates)):
        day, prev = dates[i], dates[i - 1]
        members = plate_flow.plate_members_full(code, day)
        if not members:
            continue
        rets = []
        today_map, prev_map = by_date[day], by_date[prev]
        for c in members:
            a, b = prev_map.get(c), today_map.get(c)
            if a is None or b is None or a == 0:
                continue
            rets.append(b / a - 1.0)
        if not rets:
            continue
        day_ret = sum(rets) / len(rets)
        nav *= 1.0 + day_ret
        x.append(day.replace("-", ""))
        y.append(round(nav, 4))
        coverage.append({"date": day, "members": len(rets), "membership_as_of": day})
        if len(x) >= n:
            # 已够窗口；保留尾部 n 根（继续扫到最新）
            pass
    if len(x) > n:
        x, y, coverage = x[-n:], y[-n:], coverage[-n:]
    if not x:
        return empty
    return {
        "x": x, "y": y, "vol": [], "turnover": None, "amount": None,
        "series_kind": "daily_nav", "status": "ok",
        "source": "ew_member_daily_close", "coverage": coverage,
        "frequency": "daily", "as_of_date": x[-1],
        "amount_unit": None, "turnover_semantics": None,
    }


async def plate_kline_contract(code: str, kind: str = "main", n: int = 250) -> dict:
    """板块日K统一契约 {x, y:[o,c,h,l,preclose], vol, turnover}（原站 StockKlineDay splitData）。

    数据源优先级：
    - 881/885/886 → 本地 ths_board_daily（同花顺 d10jqka 免费源，真实 bar）；
    - 883404/883957 → plate_kline 已发布快照代理（宽度/全A）；
    - 801/803/804 等 → 原站校准层（可选）真实 OHLC；缺失则免费成分等权 daily_nav。
    kind='sub' 时优先二级板块序列，缺失回退主板序列（同一代码空间）。
    """
    code = str(code or "").strip()
    empty = {"x": [], "y": [], "vol": [], "turnover": None}
    if not code:
        return empty
    from app.config import settings
    key = f"kline_plate:{kind}:{code}:{n}"
    hit = cache.get(key)
    if hit:
        return hit
    data = empty
    if code.startswith(("881", "885", "886")):
        bars = store.ths_board_daily_range(code, "1990-01-01", "2099-12-31")
        if bars:
            data = _slice_contract(_bars_to_contract(bars), n)
            data = {**data, "series_kind": "ohlc", "status": "ok", "source": "ths_board_daily"}
    elif code in ("883404", "883957"):
        proxy = await plate_kline(code)
        items = proxy.get("items") or []
        if items:
            data = _slice_contract(_items_to_contract(items), n)
            data = {**data, "series_kind": "ohlc", "status": "ok",
                    "source": proxy.get("source") or "plate_proxy"}
    elif is_plate_code(code):
        row = None
        if settings.enable_origin_reference:
            from app.datasources import zizizaizai
            today = datetime.now().strftime("%Y-%m-%d")
            row = store.plate_kline_origin_get(kind, code)
            stale = not row or str(row.get("fetched_at") or "")[:10] != today
            if stale:
                # 与股票 K 线同模式：API 进程按需回源（zizizaizai 全局限流 6.5s），当日缓存后不再打上游
                try:
                    fetch = zizizaizai.plate_kline_sub if kind == "sub" else zizizaizai.plate_kline_main
                    series = await fetch(code)
                    if series.get("x"):
                        store.plate_kline_origin_save(kind, code, series)
                        row = {**series, "fetched_at": datetime.now().isoformat(timespec="seconds")}
                except Exception:
                    pass
            if (not row or not row.get("x")) and kind == "sub":
                row = store.plate_kline_origin_get("main", code)
        if row and row.get("x"):
            data = _slice_contract({k: row.get(k) for k in ("x", "y", "vol", "turnover")}, n)
            data = {**data, "series_kind": "ohlc", "status": "ok",
                    "source": "plate_kline_origin"}
        else:
            # 免费优先：无真实板指 OHLC 时用成分等权日净值，禁止假蜡烛
            data = plate_ew_daily_nav(code, n)
    # 原站兼容字段 turnover 在板块序列中承载成交额，并非换手率。
    # 保留旧字段，同时提供有单位的明确契约，避免展示层误除以 100。
    if data.get("series_kind") != "daily_nav":
        data = {**data, "amount": data.get("turnover"),
                "turnover_semantics": "amount_cny", "amount_unit": "CNY"}
    data = {**data,
            "as_of_date": data.get("as_of_date") or ((data.get("x") or [None])[-1]),
            "frequency": data.get("frequency") or "daily"}
    if data.get("x"):
        cache.set(key, data, 300)
    return data


async def trend(code: str) -> list:
    key = f"trend:{code}"
    hit = cache.get(key)
    if hit:
        return hit
    data = await tencent.trend_minute_by_symbol(resolve_symbol(code))
    cache.set(key, data, 5)
    return data


async def realtime(codes: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for c in codes:
        key = f"rt:{c}"
        hit = cache.get(key)
        if hit:
            out[c] = hit
    missing = [c for c in codes if c not in out]
    if missing:
        try:
            got = await tencent.realtime(missing)
        except Exception:
            got = await sina.realtime(missing)
        for c, v in got.items():
            out[normalize_code(c)] = v
            cache.set(f"rt:{normalize_code(c)}", v, 5)
    return out


async def plate_rank(plate_type: int = 17, limit: int = 20) -> list[dict]:
    # 优先板块级日度快照（与原站人气 tab 数值一致）；快照缺失回退东财 boards 序列。
    if plate_type in (14, 15, 17):
        dates = store.plate_rank_dates(plate_type, limit=1)
        if dates:
            end = dates[0]
            rows = store.plate_rank_range(plate_type, end, end)
            rows.sort(key=lambda r: float(r.get('score') or 0), reverse=True)
            return rows[:limit]
    from app.services.boards import evolution
    result = await evolution(plate_type, None, 1, limit, 'pct')
    return result['columns'][0]['items'] if result['columns'] else []


# 首页指数迷你卡片：上证/深证/创业/科创50/北证50
HOME_INDICES = [
    {"code": "000001.SS", "symbol": "sh000001", "name": "上证指数"},
    {"code": "399001.SZ", "symbol": "sz399001", "name": "深证成指"},
    {"code": "399006.SZ", "symbol": "sz399006", "name": "创业板指"},
    {"code": "000688.SS", "symbol": "sh000688", "name": "科创50"},
    {"code": "899050.BJ", "symbol": "bj899050", "name": "北证50"},
]


async def index_trends() -> list[dict]:
    """5 大指数实时 + 分时。默认只读已发布快照。"""
    from app.services import index_feed
    return await index_feed.review()


PLATE_PROXY = {
    "883957": {"b_name": "同花顺全A", "note": "free proxy: published 上证A指, not THS All-A"},
    "883404": {"b_name": "同花顺情绪", "note": "local breadth proxy from published minute samples, not THS emotion index"},
}


def _iso_day(day: str) -> str:
    d = str(day or "").replace("-", "")
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else str(day or "")


async def plate_kline(code: str, date1: str | None = None, n: int = 250) -> dict:
    """VIP 大盘/情绪盘 K 线：只读已发布指数历史或分钟收盘样本，不现场打上游。"""
    from app.core.store import store
    from app.services import index_history, pools

    code = str(code or "").strip()
    start = _iso_day(date1) if date1 else None
    if code in ("883957", "000002", "sh000002"):
        snap = index_history.published(date1) if date1 else None
        if snap is None:
            snap = store.index_history_latest()
        series = ((snap or {}).get("series") or {}).get("000002") or {}
        raw_bars = list(series.get("bars") or [])
        items = []
        prev_close = None
        for bar in raw_bars:
            day = bar.get("trade_date")
            if start and day < start:
                prev_close = bar.get("close")
                continue
            items.append({
                "date": day,
                "p_open": bar.get("open"),
                "p_close": bar.get("close"),
                "p_high": bar.get("high"),
                "p_low": bar.get("low"),
                "p_prev_close": prev_close,
                "p_close_pre1d": prev_close,
                "amount": None,
                "turnover": None,
                "volume": None,
                "b_id": 883957,
                "b_name": "同花顺全A",
            })
            prev_close = bar.get("close")
        return {
            "code": "883957",
            "items": items[-n:],
            "status": "ok" if items else "missing",
            "source": "published_index_history:000002",
            "note": PLATE_PROXY["883957"]["note"],
        }

    calendar = store.kv_get("collector_calendar_v1", {}) or {}
    days = [_iso_day(x) for x in (calendar.get("days") or [])]
    if start:
        days = [d for d in days if d >= start]
    days = days[-n:]
    items = []
    prev_close = 1000.0
    for day in days:
        samples = store.minute_all(day)
        last = samples[-1] if samples else {}
        avg = last.get("avg_pct")
        amount = last.get("amount")
        if avg is None:
            if pools.published("up", day) is None and not last:
                continue
            avg = 0.0
        close = round(prev_close * (1 + float(avg) / 100.0), 3)
        high = round(max(prev_close, close), 3)
        low = round(min(prev_close, close), 3)
        items.append({
            "date": day,
            "p_open": round(prev_close, 3),
            "p_close": close,
            "p_high": high,
            "p_low": low,
            "p_prev_close": round(prev_close, 3),
            "p_close_pre1d": round(prev_close, 3),
            "amount": amount,
            "turnover": amount,
            "volume": amount,
            "b_id": 883404,
            "b_name": "同花顺情绪",
            "quote_rate": float(avg),
        })
        prev_close = close
    return {
        "code": "883404",
        "items": items,
        "status": "ok" if items else "missing",
        "source": "published_minute_samples_breadth_proxy",
        "note": PLATE_PROXY["883404"]["note"],
    }
