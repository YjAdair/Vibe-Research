"""精选板块(801xxx)资金流聚合：易筋经 z tab 数据链路。

主口径（plate_rank_daily，优先）：
- 板块级日度排行快照（score/money_leader/rate/speed/volume_ration 等完整字段），
  与原站数值逐值一致；worker 收盘后通过 zizizaizai 数据源采集三类(14/15/17)。
  历史回填：python -m app.services.plate_rank_backfill

rank/days 只读 plate_rank_daily；缺少完整板块快照时返回空结果。
旧个股聚合代码的均值/z-score 口径不再作为同名 sum_rate/sum_score 输出，避免把
自定义指标伪装成原站数据。
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP
from pathlib import Path

from app.core.store import store

MEMBERS_PATH = Path(__file__).resolve().parents[2] / 'data' / 'plate17_members_snapshot.json'
FULL_MEMBERS_PATH = Path(__file__).resolve().parents[2] / 'data' / 'plate17_members_full.json'

_members_cache: dict | None = None
_members_mtime: float = 0.0
_full_members_cache: dict | None = None
_full_members_mtime: float = 0.0


def load_members() -> dict:
    """801 板块 -> {'plate_name': str, 'stocks': {date: {sub_code: [codes]}}}"""
    global _members_cache, _members_mtime
    if not MEMBERS_PATH.exists():
        return {}
    mtime = MEMBERS_PATH.stat().st_mtime
    if _members_cache is not None and mtime == _members_mtime:
        return _members_cache
    data = json.loads(MEMBERS_PATH.read_text(encoding='utf-8'))
    plates = data.get('plates') or {}
    _members_cache = plates
    _members_mtime = mtime
    return plates


def load_stock_names() -> dict[str, str]:
    """成分快照里缓存的 code→名称（开盘啦名单带回）。"""
    if not MEMBERS_PATH.exists():
        return {}
    try:
        data = json.loads(MEMBERS_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    names = data.get('stock_names') or {}
    return {str(k): str(v) for k, v in names.items() if k and v}


def stock_display_name(code: str, *fallbacks: str | None) -> str:
    for v in fallbacks:
        if v and str(v) != str(code):
            return str(v)
    hit = load_stock_names().get(str(code))
    if hit:
        return hit
    return str(code)


def _nearest_member_day(stocks_by_date: dict, date: str) -> str | None:
    """优先 <=date 最新快照；尚无更早日时回退最早可用日（切片名单暂未按日回填）。"""
    if not stocks_by_date:
        return None
    before = sorted(d for d in stocks_by_date if d <= date)
    if before:
        return before[-1]
    # ponytail: 单日切片反推历史列，按日历史齐了删此回退
    return sorted(stocks_by_date)[0]


def plate_members(code: str, date: str) -> set[str]:
    """该板块在最近成分快照中的全部成分股代码（跨子板块去重）。"""
    plates = load_members()
    v = plates.get(code) or {}
    stocks_by_date = v.get('stocks') or {}
    day = _nearest_member_day(stocks_by_date, date)
    if not day:
        return set()
    latest = stocks_by_date[day] or {}
    members: set[str] = set()
    for arr in latest.values():
        members.update(arr or [])
    return members


def load_full_members() -> dict:
    """801/803 板块 -> {'captured_at': str, 'stocks': [codes]}。

    来源：原站 /v3/market/plates/17/{code}/stocks/rank?is_real=1&limit=2000
    一次性全量快照（含不挂任何子板块的直属成分，子板块并集覆盖不全，
    如 801085 子板块并集 831 只 vs 全量 1527 只）。
    入库脚本：scripts/ingest_full_members.py。商业化前需替换为自有 THS 成分源。
    """
    global _full_members_cache, _full_members_mtime
    if not FULL_MEMBERS_PATH.exists():
        return {}
    mtime = FULL_MEMBERS_PATH.stat().st_mtime
    if _full_members_cache is not None and mtime == _full_members_mtime:
        return _full_members_cache
    data = json.loads(FULL_MEMBERS_PATH.read_text(encoding='utf-8'))
    _full_members_cache = data.get('plates') or {}
    _full_members_mtime = mtime
    return _full_members_cache


def plate_members_full(code: str, date: str) -> set[str]:
    """板块全部成分：历史子板块并集，必要时并入不晚于 date 的全量快照。

    全量快照只有在其 captured_at <= date 时才可用于该历史日期；否则只能使用
    已有的历史子板块并集，避免把未来成分泄漏到历史计算。
    """
    members = plate_members(code, date)
    full = load_full_members().get(code) or {}
    captured_at = str(full.get('captured_at') or '')
    try:
        captured_date = datetime.strptime(captured_at, '%Y-%m-%d').date()
        target_date = datetime.strptime(date, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        captured_date = target_date = None
    if captured_date is not None and target_date is not None and captured_date <= target_date:
        members |= {str(c) for c in (full.get('stocks') or []) if c}
    return members


_rank_day_cache: dict[tuple[int, str], dict] = {}


def _rank_rows_by_day(plate_type: int, day: str) -> dict:
    key = (plate_type, day)
    hit = _rank_day_cache.get(key)
    if hit is not None:
        return hit
    rows = store.plate_rank_range(plate_type, day, day)
    out = {str(r['plate_code']): r for r in rows if r.get('plate_code')}
    if len(_rank_day_cache) > 400:
        _rank_day_cache.clear()
    _rank_day_cache[key] = out
    return out


def plate_rank_score(plate_code: str, date: str, plate_type: int = 17) -> tuple:
    """<=date 最新板块日度排行快照的 (score, plate_name)；无快照时 (None, 目录名)。"""
    row = _rank_rows_by_day(plate_type, date).get(plate_code)
    if row is None:
        for d in store.plate_rank_dates(plate_type, limit=200):
            if d > date:
                continue
            row = _rank_rows_by_day(plate_type, d).get(plate_code)
            if row:
                break
    if not row:
        return None, plate_name(plate_code)
    return row.get('score'), row.get('plate_name') or plate_name(plate_code)


def _plate_snapshot(code: str, date: str) -> dict | None:
    """最近成分快照（含 sub_plates 与各子板块成分）。"""
    plates = load_members()
    v = plates.get(code) or {}
    stocks_by_date = v.get('stocks') or {}
    day = _nearest_member_day(stocks_by_date, date)
    if not day:
        return None
    return {'sub_plates': v.get('sub_plates') or [], 'date': day,
            'sub_stocks': stocks_by_date[day] or {}}


def _pool_codes(pool: str, trade_date: str) -> set[str]:
    payload = store.limit_pool_get(pool, trade_date) or {}
    return {str(s.get('c') or '') for s in (payload.get('pool') or []) if s.get('c')}


def _limit_price(prev_close, code: str, up: bool) -> Decimal:
    """涨跌停价 = prev_close × (1±r)，0.01 位四舍五入（ROUND_HALF_UP）。

    r 按纯代码（原站口径，2026-09-13 与 295/295 子板块×日期对全匹配 0 误差）：
    4/8/9 开头=30%（北交所），300/301/688/689=20%（创业板/科创板），其余=10%。
    无 ST 5% 特例；新股（N 前缀首日大涨）因 close ≠ 涨停价自然排除。
    """
    if code[:1] in ('4', '8', '9'):
        r = Decimal('0.30')
    elif code[:3] in ('300', '301', '688', '689'):
        r = Decimal('0.20')
    else:
        r = Decimal('0.10')
    factor = Decimal('1') + (r if up else -r)
    return (Decimal(str(prev_close)) * factor).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _listing_day_codes(d: str, quotes: dict[str, dict]) -> set[str]:
    """首日新挂牌股票代码集合：当日快照名 N 前缀 且 daily_close 无早于 d 的记录。

    原站 stats.quote_rate 数据源不含首日新股（801881 2026-09-11 剔除
    688801 N燧原-U +179% 后与原站精确一致 -1.71）；首日涨幅相对发行价，
    无涨跌幅约束，纳入均值会严重失真。复牌股有早期记录，不受影响。
    """
    cands = [c for c, q in quotes.items() if str(q.get('stock_name') or '')[:1] == 'N']
    if not cands:
        return set()
    prior = store.daily_close_latest_before(cands, d)
    return {c for c in cands if str((prior.get(c) or {}).get('trade_date') or d) >= d}


def _valid_price(value) -> bool:
    """是否为可用于涨幅计算的正价格。"""
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _stock_rate_row(code: str, quote: dict, fallback: dict | None = None) -> dict:
    """生成个股涨幅字段，不用旧收盘伪造 0 涨幅。"""
    quote = quote or {}
    fallback = fallback or {}
    close = quote.get('close')
    prev = quote.get('prev_close')
    if _valid_price(close) and _valid_price(prev):
        return {
            'px_change_rate': float(f'{(float(close) / float(prev) - 1) * 100:.6g}'),
            'last_px': round(float(close), 2),
            'data_status': 'ok',
        }
    if _valid_price(close):
        return {
            'px_change_rate': None,
            'last_px': round(float(close), 2),
            'data_status': 'missing_prev_close',
        }
    fallback_close = fallback.get('close')
    if _valid_price(fallback_close):
        return {
            'px_change_rate': None,
            'last_px': round(float(fallback_close), 2),
            'data_status': 'missing_current_quote',
        }
    return {'px_change_rate': None, 'last_px': None, 'data_status': 'missing'}


def _sort_rate_rows(rows: list[dict]) -> None:
    """有效涨幅优先；无涨幅数据统一置后，仍按已知现价降序。"""
    rows.sort(key=lambda r: (
        r['px_change_rate'] is None,
        -(r['px_change_rate'] if r['px_change_rate'] is not None else 0),
        -(r['last_px'] if r['last_px'] is not None else 0),
        r['stock_code'],
    ))


# 累计窗口分档：窗口越长，入档门槛与档位越高（投研波段强度；10 日对齐原站）。
# days 为其它值时回退 10 日方案，兼容旧调用。
_PCT_SCHEMES: dict[int, tuple[float, list[str]]] = {
    5: (10.0, ["10-15", "15-20", "20-30", "30-40", "40+"]),
    10: (20.0, ["20-40", "40-60", "60-80", "80-100", "100+"]),
    20: (30.0, ["30-50", "50-80", "80-100", "100-150", "150+"]),
}


def _pct_scheme(days: int) -> tuple[float, list[str]]:
    return _PCT_SCHEMES.get(days, _PCT_SCHEMES[10])


def _interval_of(pct: float, days: int = 10) -> str | None:
    """按窗口方案分桶；边界左闭右开，最高档右开为 +∞。"""
    min_pct, intervals = _pct_scheme(days)
    if pct < min_pct:
        return None
    for iv in intervals:
        if iv.endswith("+"):
            lo = float(iv[:-1])
            if pct >= lo:
                return iv
            continue
        lo_s, hi_s = iv.split("-", 1)
        lo, hi = float(lo_s), float(hi_s)
        if lo <= pct < hi:
            return iv
    return intervals[-1]


def _cash_events_between(code: str, base_date: str, end_date: str) -> list[dict]:
    """窗口 (base_date, end_date] 内已核实的纯现金分红事件。"""
    rec = store.kv_get(f"corp_actions:{code}") or {}
    if not isinstance(rec, dict) or not rec.get("verified"):
        return []
    out = []
    for ev in rec.get("events") or []:
        if not isinstance(ev, dict) or not ev.get("verified") or not ev.get("complete"):
            continue
        if str(ev.get("kind") or "").lower() != "cash":
            continue
        ex = ev.get("ex_date")
        if not isinstance(ex, str) or not (base_date < ex <= end_date):
            continue
        cash = ev.get("cash_ps", ev.get("cash_dividend", ev.get("cash")))
        try:
            cash_f = float(cash)
        except (TypeError, ValueError):
            continue
        if cash_f <= 0:
            continue
        out.append({"ex_date": ex, "cash_ps": cash_f})
    out.sort(key=lambda x: x["ex_date"])
    return out


def _qfq_base_close(
    code: str,
    base_close: float,
    base_date: str,
    end_date: str,
    *,
    closes_by_date: dict[str, float] | None = None,
) -> tuple[float, float]:
    """跨除息窗口把基期收盘按等比前复权到期末口径。

    因子 = Π (除息日前收盘 − 每股现金) / 除息日前收盘。
    通达创智 001368：09-23 每 10 派 4，10 日累计与原站 40.55 逐值一致。
    返回 (adjusted_base, factor)；无事件时 factor=1。
    """
    events = _cash_events_between(code, base_date, end_date)
    if not events:
        return float(base_close), 1.0
    factor = 1.0
    for ev in events:
        ex = ev["ex_date"]
        cash = ev["cash_ps"]
        ref = None
        if closes_by_date:
            prev_dates = [d for d in closes_by_date if d < ex]
            if prev_dates:
                ref = closes_by_date[max(prev_dates)]
        if ref is None:
            prior = store.daily_close_latest_before([code], ex)
            hit = (prior or {}).get(code) or {}
            ref = hit.get("close")
        try:
            ref_f = float(ref)
        except (TypeError, ValueError):
            return float(base_close), 1.0
        if ref_f <= cash:
            return float(base_close), 1.0
        factor *= (ref_f - cash) / ref_f
    return float(base_close) * factor, factor


def _stocks_pct_from_maps(
    members: set[str] | list[str],
    end_map: dict[str, dict],
    base_map: dict[str, dict],
    days: int,
    *,
    base_date: str,
    end_source: str,
    end_date: str | None = None,
    hist_closes: dict[str, dict[str, float]] | None = None,
) -> dict:
    min_pct, intervals = _pct_scheme(days)
    out: dict[str, list[dict]] = {iv: [] for iv in intervals}
    end_date = end_date or base_date
    for code in members:
        e, b = end_map.get(code), base_map.get(code)
        if not e or not b:
            continue
        c_end = e.get("close")
        c_base = b.get("close")
        if not c_end or not c_base:
            continue
        closes = (hist_closes or {}).get(str(code))
        adj_base, _factor = _qfq_base_close(
            str(code), float(c_base), base_date, end_date, closes_by_date=closes,
        )
        if adj_base <= 0:
            continue
        pct = round((float(c_end) / adj_base - 1) * 100, 2)
        iv = _interval_of(pct, days)
        if not iv:
            continue
        out[iv].append({
            "stock_code": code,
            "stock_name": stock_display_name(code, e.get("stock_name"), b.get("stock_name")),
            "cum_pct": pct,
        })
    for arr in out.values():
        arr.sort(key=lambda s: (-s["cum_pct"], s["stock_code"]))
    return {
        "intervals": intervals,
        "stocks": out,
        "meta": {"days": days, "min_pct": min_pct, "base_date": base_date, "end_source": end_source},
    }


def stocks_pct(plate_code: str, date1: str, days: int) -> dict:
    """题材成分 N 日累计涨幅分档。

    cum_pct = (date1 收盘 / N 个交易日前收盘 - 1) * 100，round 2。
    分档随 days 变化：5 → ≥10%/40+…10+；10 → ≥20% 原站五档；20 → ≥30%/150+…30+。
    桶内 cum_pct 降序、同值代码升序。
    今日缺收盘日线时请走 stocks_pct_resolved（盘中价作期末）。
    """
    min_pct, intervals = _pct_scheme(days)
    empty = {
        "intervals": intervals,
        "stocks": {iv: [] for iv in intervals},
        "meta": {"days": days, "min_pct": min_pct},
    }
    members = plate_members(plate_code, date1)
    if not members:
        return empty
    rows = store.daily_close_range(date1, max(days, 1))
    by_date: dict[str, dict[str, dict]] = {}
    for r in rows:
        by_date.setdefault(r["trade_date"], {})[str(r["stock_code"])] = r
    dates_have = sorted(by_date)
    if date1 not in by_date or len(dates_have) < days + 1:
        return empty
    base_date = dates_have[dates_have.index(date1) - days]
    hist_closes: dict[str, dict[str, float]] = {}
    for d, m in by_date.items():
        for c, row in m.items():
            if isinstance(row.get("close"), (int, float)):
                hist_closes.setdefault(str(c), {})[d] = float(row["close"])
    return _stocks_pct_from_maps(
        members, by_date[date1], by_date.get(base_date) or {}, days,
        base_date=base_date, end_source="daily_close", end_date=date1,
        hist_closes=hist_closes,
    )


async def stocks_pct_resolved(plate_code: str, date1: str, days: int) -> dict:
    """累计涨幅：有收盘日线走日线；今日缺日线时用腾讯实时价作期末（对齐原站盘中可算）。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.datasources import tencent

    min_pct, intervals = _pct_scheme(days)
    empty = {
        "intervals": intervals,
        "stocks": {iv: [] for iv in intervals},
        "meta": {"days": days, "min_pct": min_pct},
    }
    base = stocks_pct(plate_code, date1, days)
    if any(base.get("stocks", {}).get(iv) for iv in intervals):
        return base
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    if date1 != today:
        return base
    members = plate_members(plate_code, date1)
    if not members:
        return empty
    # 取 date1 之前的日线作基准（不含今日）
    hist = store.daily_close_range(date1, max(days + 2, 3))
    by_date: dict[str, dict[str, dict]] = {}
    for r in hist:
        d = r["trade_date"]
        if d >= date1:
            continue
        by_date.setdefault(d, {})[str(r["stock_code"])] = r
    dates_have = sorted(by_date)
    if len(dates_have) < days:
        return empty
    base_date = dates_have[-days]
    base_map = by_date.get(base_date) or {}
    member_list = sorted(members)
    live: dict[str, dict] = {}
    for i in range(0, len(member_list), 60):
        try:
            live.update(await tencent.realtime(member_list[i:i + 60]))
        except Exception:
            continue
    end_map: dict[str, dict] = {}
    for c in member_list:
        lq = live.get(c) or {}
        price = lq.get("price")
        if not _valid_price(price):
            continue
        end_map[c] = {"close": float(price), "stock_name": lq.get("name")}
    if not end_map:
        return empty
    hist_closes: dict[str, dict[str, float]] = {}
    for d, m in by_date.items():
        for c, row in m.items():
            if isinstance(row.get("close"), (int, float)):
                hist_closes.setdefault(str(c), {})[d] = float(row["close"])
    return _stocks_pct_from_maps(
        members, end_map, base_map, days,
        base_date=base_date, end_source="realtime", end_date=date1,
        hist_closes=hist_closes,
    )


def stocks_pct_batch(plate_code: str, dates: list[str], days: int) -> dict:
    """原站 /v3/market/plates/17/{code}/stocks/pct/batch 复刻（VIP）。

    响应外层即 {date: {intervals, stocks}}（原站前端直接 pctDataMap = data.data）。
    """
    return {d: stocks_pct(plate_code, d, days) for d in dates}


def _rates_rows_local(member_list: list[str], date1: str) -> list[dict]:
    quotes = store.daily_close_by_codes(date1, member_list)
    missing = [c for c in member_list if not _valid_price((quotes.get(c) or {}).get('close'))]
    fallbacks = store.daily_close_latest_before(missing, date1) if missing else {}
    rows = []
    for c in member_list:
        q = quotes.get(c) or {}
        fallback = fallbacks.get(c) or {}
        row = {
            'stock_code': c,
            'stock_name': stock_display_name(c, q.get('stock_name'), fallback.get('stock_name')),
        }
        row.update(_stock_rate_row(c, q, fallback))
        rows.append(row)
    return rows


def _paginate_rate_rows(rows: list[dict], page: int, limit: int) -> dict:
    _sort_rate_rows(rows)
    total = len(rows)
    start = max(0, page - 1) * limit
    return {'list': rows[start:start + limit], 'total': total, 'page': page, 'limit': limit}


# is_real 全成分拉行情很重；短 TTL 避免题材轮动多列/翻页把单进程 API 打挂
_REALTIME_RATES_CACHE: dict[str, tuple[float, list[dict]]] = {}
_REALTIME_RATES_TTL = 45.0


async def _rates_rows_realtime_cached(member_list: list[str], date1: str, cache_key: str) -> list[dict]:
    import time
    now = time.time()
    hit = _REALTIME_RATES_CACHE.get(cache_key)
    if hit and now - hit[0] < _REALTIME_RATES_TTL:
        return [dict(r) for r in hit[1]]
    rows = await _rates_rows_realtime(member_list, date1)
    _REALTIME_RATES_CACHE[cache_key] = (now, rows)
    if len(_REALTIME_RATES_CACHE) > 64:
        oldest = sorted(_REALTIME_RATES_CACHE.items(), key=lambda kv: kv[1][0])[:32]
        for k, _ in oldest:
            _REALTIME_RATES_CACHE.pop(k, None)
    return [dict(r) for r in rows]


async def _rates_rows_realtime(member_list: list[str], date1: str) -> list[dict]:
    """最新交易日：腾讯实时价/涨幅覆盖；缺实时再回落本地日线。"""
    from app.datasources import tencent

    live: dict[str, dict] = {}
    for i in range(0, len(member_list), 60):
        chunk = member_list[i:i + 60]
        try:
            live.update(await tencent.realtime(chunk))
        except Exception:
            continue
    local_rows = {r['stock_code']: r for r in _rates_rows_local(member_list, date1)}
    rows = []
    for c in member_list:
        lq = live.get(c) or {}
        price, prev, pct = lq.get('price'), lq.get('prev_close'), lq.get('pct')
        name = stock_display_name(c, lq.get('name'), (local_rows.get(c) or {}).get('stock_name'))
        if _valid_price(price) and pct is not None:
            rows.append({
                'stock_code': c,
                'stock_name': name,
                'px_change_rate': float(f'{float(pct):.6g}'),
                'last_px': round(float(price), 2),
                'data_status': 'realtime',
            })
        elif _valid_price(price) and _valid_price(prev):
            rows.append({
                'stock_code': c,
                'stock_name': name,
                'px_change_rate': float(f'{(float(price) / float(prev) - 1) * 100:.6g}'),
                'last_px': round(float(price), 2),
                'data_status': 'realtime',
            })
        else:
            rows.append(local_rows.get(c) or {
                'stock_code': c, 'stock_name': name,
                'px_change_rate': None, 'last_px': None, 'data_status': 'missing',
            })
    return rows


def sub_plate_stocks_rates(sub_code: str, date1: str, page: int, limit: int) -> dict:
    """原站 /v3/market/plates/18/{code}/stocks/rates 复刻（子板块个股涨幅榜）。

    成分：members snapshot 中任一 801 板块下该子板块最近成分。
    px_change_rate = (close/prev_close-1)*100，6 位有效数字；排序有效涨幅降序。
    """
    plates = load_members()
    members: set[str] = set()
    for v in plates.values():
        stocks_by_date = v.get('stocks') or {}
        day = _nearest_member_day(stocks_by_date, date1)
        if not day:
            continue
        latest = stocks_by_date[day] or {}
        members.update(latest.get(sub_code) or [])
    if not members:
        return {'list': [], 'total': 0, 'page': page, 'limit': limit}
    rows = _rates_rows_local(sorted(members), date1)
    return _paginate_rate_rows(rows, page, limit)


async def sub_plate_stocks_rates_resolved(sub_code: str, date1: str, page: int, limit: int,
                                          is_real: bool = False) -> dict:
    plates = load_members()
    members: set[str] = set()
    for v in plates.values():
        stocks_by_date = v.get('stocks') or {}
        day = _nearest_member_day(stocks_by_date, date1)
        if not day:
            continue
        latest = stocks_by_date[day] or {}
        members.update(latest.get(sub_code) or [])
    if not members:
        return {'list': [], 'total': 0, 'page': page, 'limit': limit}
    member_list = sorted(members)
    if is_real:
        rows = await _rates_rows_realtime_cached(
            member_list, date1, f"sub:{sub_code}:{date1}:{len(member_list)}")
    else:
        rows = _rates_rows_local(member_list, date1)
    return _paginate_rate_rows(rows, page, limit)


def _plate_members_union(plate_code: str, date1: str) -> set[str]:
    """plates/17 板块全成分：子板块并集（优先全量快照）。"""
    return plate_members_full(plate_code, date1)


def plate_stocks_rates(plate_code: str, date1: str, page: int, limit: int) -> dict:
    """原站 /v3/market/plates/17/{code}/stocks/rates 复刻（板块全成分个股涨幅榜）。"""
    members = _plate_members_union(plate_code, date1)
    if not members:
        return {'list': [], 'total': 0, 'page': page, 'limit': limit}
    rows = _rates_rows_local(sorted(members), date1)
    return _paginate_rate_rows(rows, page, limit)


async def plate_stocks_rates_resolved(plate_code: str, date1: str, page: int, limit: int,
                                      is_real: bool = False) -> dict:
    members = _plate_members_union(plate_code, date1)
    if not members:
        return {'list': [], 'total': 0, 'page': page, 'limit': limit}
    member_list = sorted(members)
    if is_real:
        rows = await _rates_rows_realtime_cached(
            member_list, date1, f"plate:{plate_code}:{date1}:{len(member_list)}")
    else:
        rows = _rates_rows_local(member_list, date1)
    return _paginate_rate_rows(rows, page, limit)


async def plate_stocks_popular_rank(plate_code: str, date1: str, page: int, limit: int,
                              with_pct: int = 1, plate_type: int = 17) -> dict:
    """原站 /v3/market/plates/{pt}/{code}/stocks/rank 复刻（板块成分人气榜）。

    原站实测口径（2026-09-11，801660/801033/801001 三板块验证）：
    - 成分：与 stocks/rates 相同的板块全成分
    - 人气：当日全市场独立人气快照（rank/attention/rank_diff）；缺日≠挪用邻日
    - total = min(成分 ∩ 人气表, 300)：top 300 截断（801660 成分∩榜=317 只
      只返回 300，第 300 名 rank=1754 与原站逐位一致）
    - 排序：rank 升序（人气优先）
    - px_change_rate：daily_close 真实 prev_close 涨幅，6 位有效数字（with_pct=1 时）
    - 首日新股不再使用原站 1.00 面额占位，避免 39600% 等错误
    - 无行情但有人气的成分保留，涨幅为 null，并带 data_status 与可知 last_px

    路径 A：优先 THS 已采集 Top100 快照；缺日则东财 getHisList 按成分拼装（source=em_hist）。
    """
    from app.services import popular as popular_svc
    empty = {'list': [], 'total': 0, 'page': page, 'limit': limit}
    if plate_type == 18:
        # 子板块：成分 = 任一板块下该子板块最近成分（同 sub_plate_stocks_rates）
        plates = load_members()
        members = set()
        for v in plates.values():
            stocks_by_date = v.get('stocks') or {}
            day = _nearest_member_day(stocks_by_date, date1)
            if not day:
                continue
            latest = stocks_by_date[day] or {}
            members.update(latest.get(plate_code) or [])
    else:
        members = _plate_members_union(plate_code, date1)
    if not members:
        return {**empty, 'status': 'missing_members',
                'meta': {'status': 'missing_members', 'note': '缺少该日题材成分'}}
    # 人气按日定格：缺日不借邻日（缺失≠旧榜）；THS 缺则 em_hist
    snap = popular_svc.published(date1, allow_stale=False)
    if not snap or (snap.get('date') and snap.get('date') != date1):
        snap = await popular_svc.em_hist_snapshot(members, date1)
    if not snap or (snap.get('date') and snap.get('date') != date1):
        return {**empty, 'status': 'missing_popular_snapshot',
                'meta': {'status': 'missing_popular_snapshot',
                         'note': '缺少该日人气：无 THS Top100 快照且东财个股历史亦无该日'}}
    pop = {i['symbol_code']: i for i in snap.get('items') or [] if i.get('symbol_code')}
    pop_names = {i.get('symbol_code'): i.get('symbol_name') or '' for i in snap.get('items') or []}
    hit = [c for c in sorted(members) if c in pop]
    hit.sort(key=lambda c: (pop[c].get('rank') or 0, c))
    hit = hit[:300]
    total = len(hit)
    start = max(0, page - 1) * limit
    page_codes = hit[start:start + limit]
    pool = len(snap.get('items') or [])
    src = snap.get('source')
    if src == 'em_hist':
        meta = {
            'status': 'ok',
            'source': 'em_hist',
            'rank_pool_size': None,
            'rank_scope': 'em_guba_full_market',
            'note': '人气=东财个股历史rank拼装∩题材成分；非THS Top100，非源站深表',
            'popularity_collected_at': snap.get('collected_at'),
            'rank_change_status': 'derived_prev_em_day',
        }
    else:
        meta = {
            'status': 'ok',
            'source': src,
            'rank_pool_size': pool,
            'rank_scope': 'vendor_published_pool' if pool < 1000 else 'origin_ths_top_deep',
            'note': (
                f'人气=公开榜Top{pool}∩题材成分，非板块全体深榜'
                if pool < 1000
                else f'人气=源站ths/top深表({pool})∩题材成分，截断300（过渡授权）'
            ),
            'popularity_collected_at': snap.get('collected_at'),
            'rank_change_status': (
                'verified' if snap.get('rank_diff_basis') == 'previous_trading_day_same_time'
                else 'provider_raw'
            ),
        }
    if not page_codes:
        return {**empty, 'total': total, 'status': 'ok', 'meta': meta}
    quotes = store.daily_close_by_codes(date1, page_codes)
    missing = [c for c in page_codes if not _valid_price((quotes.get(c) or {}).get('close'))]
    fallbacks = store.daily_close_latest_before(missing, date1) if missing else {}
    rows = []
    for c in page_codes:
        p = pop[c]
        q = quotes.get(c) or {}
        # rank_diff：供应商原值保留；None 不改写成 0（0 才表示名次无变化）
        row = {
            'stock_name': stock_display_name(
                c, p.get('symbol_name'), pop_names.get(c), q.get('stock_name')
            ),
            'stock_code': c,
            'rank': p.get('rank'),
            'rank_diff': p.get('rank_diff'),
            'attention': p.get('heat') if p.get('heat') is not None else p.get('attention'),
            'last_pct': None,
        }
        if with_pct:
            row.update(_stock_rate_row(c, q, fallbacks.get(c)))
        rows.append(row)
    # 今日缺收盘日线时补实时涨幅，避免人气列全 0.00%
    if with_pct:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        need_live = [r['stock_code'] for r in rows if r.get('px_change_rate') is None]
        if date1 == today and need_live:
            live_rows = await _rates_rows_realtime_cached(
                need_live, date1, f'popular-live:{date1}:{len(need_live)}')
            live_map = {r['stock_code']: r for r in live_rows}
            for r in rows:
                lv = live_map.get(r['stock_code'])
                if not lv or lv.get('px_change_rate') is None:
                    continue
                r['px_change_rate'] = lv.get('px_change_rate')
                r['last_px'] = lv.get('last_px')
                r['data_status'] = lv.get('data_status') or 'realtime'
    return {'list': rows, 'total': total, 'page': page, 'limit': limit, 'status': 'ok', 'meta': meta}


def sub_plates_stocks(plate_code: str, dates: list[str]) -> dict:
    """原站 /v3/market/plates/17/{code}/sub-plates-stocks 复刻。

    成分：members snapshot 按请求日期逐一解析（<= 该日期的最新快照）。原站按日期
    存成分，历史日期返回当时成分（2026-09-13 与 801660/801085/803023 五日对拍确认），
    本地快照同样按日期存储，必须逐日取，不能复用最新快照。
    stats.quote_rate：子板块成分股当日涨跌幅简单均值（close/prev_close-1，round 2），
    剔除北交所（4/8/9 开头）与无行情成分（分子分母同时剔除，原站 R2 口径，
    2026-09-13 与 801216/801715/801827 等缺行情案例逐值验证）。
    另剔除首日新股（_listing_day_codes，801881 2026-09-11 案例精确验证）。
    残余约 10/295 案例与原站差 0.02~0.11pp：原站 stats 与其自身 stocks/rates
    均值亦不一致（独立板块行情源所致），无法逐值复刻，见 docs/待解决问题清单.md。
    stats.limit_up/down_count：成分股 close == 涨跌停价（_limit_price）严格相等计数，
    不依赖东财涨跌停池（em_up/em_down 池有缺日且口径不符；相等规则 295/295 全匹配）。
    无 sub_plates 的板块：原站返回 stocks={date:{}} 且不带 stats 字段；
    EM_MAPPED（东财概念映射键）仅用于内部聚合，不出现在本响应中。
    """
    dates = sorted({d for d in dates if d}) or []
    if not dates:
        return {'sub_plates': [], 'stocks': {}}
    latest = _plate_snapshot(plate_code, dates[-1])
    sub_plates = (latest or {}).get('sub_plates') or []
    if not sub_plates:
        return {'sub_plates': [], 'stocks': {d: {} for d in dates}}
    stocks_out: dict[str, dict[str, list[str]]] = {}
    stats_out: dict[str, dict[str, dict]] = {}
    for d in dates:
        snap_d = _plate_snapshot(plate_code, d)
        sub_stocks = (snap_d or {}).get('sub_stocks') or {}
        if not snap_d:
            stocks_out[d] = {}
            stats_out[d] = {}
            continue
        stocks_out[d] = {sub['code']: list(sub_stocks.get(sub['code']) or []) for sub in sub_plates}
        day_stats: dict[str, dict] = {}
        all_codes = sorted({c for sub in sub_plates for c in (sub_stocks.get(sub['code']) or [])})
        quotes_d = store.daily_close_by_codes(d, all_codes)
        listing = _listing_day_codes(d, quotes_d)
        for sub in sub_plates:
            sc = sub['code']
            codes = sub_stocks.get(sc) or []
            rates: list[float] = []
            up_n = dn_n = 0
            for c in codes:
                q = quotes_d.get(c) or {}
                close, prev = q.get('close'), q.get('prev_close')
                if not close or not prev:
                    # 无行情成分：quote_rate 分子分母同时剔除，涨跌停不计数
                    continue
                cl = Decimal(str(close))
                if cl == _limit_price(prev, c, up=True):
                    up_n += 1
                if cl == _limit_price(prev, c, up=False):
                    dn_n += 1
                if c[:1] not in ('4', '8', '9') and c not in listing:
                    rates.append((float(close) / float(prev) - 1) * 100)
            day_stats[sc] = {
                'quote_rate': round(sum(rates) / len(rates), 2) if rates else 0,
                'limit_up_count': up_n,
                'limit_down_count': dn_n,
            }
        stats_out[d] = day_stats
    return {'sub_plates': sub_plates, 'stocks': stocks_out, 'stats': stats_out}


def save_members_snapshot(plate_code: str, day: str, data: dict) -> int:
    """把原站当日成分合并进 members snapshot（按日期增量，保留历史）。

    data 为原站 sub-plates-stocks 响应 data 部分。只合并 sub_plates 非空的板块
    （叶子板块无子板块结构，原站也无）；EM_MAPPED 板块保持自建映射不被覆盖。
    """
    sub_plates = data.get('sub_plates') or []
    stocks = (data.get('stocks') or {}).get(day) or {}
    if not sub_plates or not stocks:
        return 0
    snapshot = {'meta': {}, 'plates': {}}
    if MEMBERS_PATH.exists():
        try:
            snapshot = json.loads(MEMBERS_PATH.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            snapshot = {'meta': {}, 'plates': {}}
    plates = snapshot.setdefault('plates', {})
    entry = plates.setdefault(plate_code, {'sub_plates': sub_plates, 'stocks': {}})
    entry['sub_plates'] = sub_plates
    entry.setdefault('stocks', {})[day] = stocks
    meta = snapshot.setdefault('meta', {})
    meta['members_updated_at'] = day
    plates[plate_code] = entry
    MEMBERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MEMBERS_PATH.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding='utf-8')
    tmp.replace(MEMBERS_PATH)
    global _members_cache, _members_mtime
    _members_cache = None
    _members_mtime = 0.0
    return len(stocks)


async def collect_members(day: str, plate_codes: list[str] | None = None) -> dict:
    """采集当日子板块成分快照（原站口径，开发期校准）。

    plate_codes 缺省取 snapshot 中已有 curated 子板块结构的板块（69 个，
    每个一个请求，限流 10 req/min 共约 7.5 分钟，在 worker 180s 超时内
    需分批：本函数按次序采集，调用方需控制单次板块数）。
    """
    from app.datasources import zizizaizai
    if plate_codes is None:
        plates = load_members()
        plate_codes = [c for c, v in plates.items() if v.get('sub_plates')]
    saved, errors = 0, []
    for code in plate_codes:
        try:
            data = await zizizaizai.sub_plates_stocks(code, [day])
        except Exception as exc:  # noqa: BLE001
            errors.append({'plate_code': code, 'error': type(exc).__name__})
            continue
        n = save_members_snapshot(code, day, data)
        if n:
            saved += 1
    if not saved and plate_codes:
        raise ValueError(f'members collection failed: {errors[:3]}')
    return {'date': day, 'saved': saved, 'total': len(plate_codes), 'errors': errors}


def plate_name(code: str) -> str:
    # 优先从 ths_plate_catalog 拿名称（与人气榜共用目录）
    from app.datasources.ths import plate_catalog
    rev = {v: k for k, v in (plate_catalog().get('17') or {}).items()}
    return rev.get(code, '')


async def collect_flow(day: str) -> dict:
    """收盘后采集全市场资金流快照。day 为 ISO 日期。"""
    from app.datasources import eastmoney
    snap = await eastmoney.market_flow_snapshot()
    rows = snap['rows']
    if not rows or len(rows) != snap.get('total'):
        raise ValueError('market flow snapshot empty')
    stamps = [r['source_timestamp'] for r in rows if r.get('source_timestamp')]
    from zoneinfo import ZoneInfo
    from app.services.ml_r1 import verified_close_time
    tz = ZoneInfo('Asia/Shanghai')
    for r in rows:
        r['source_as_of'] = datetime.fromtimestamp(r['source_timestamp'], tz).isoformat() if r.get('source_timestamp') else None
        if not verified_close_time(r['source_as_of'], day):
            r['main_net_inflow'] = None
    count = store.stock_flow_save(day, rows, {
        'source': 'eastmoney_push2delay_f62', 'as_of': snap.get('as_of'), 'total': snap.get('total'),
        'collected_at': datetime.now(tz).isoformat(), 'batch_complete': True,
        'verified_rows': sum(r.get('main_net_inflow') is not None for r in rows),
    })
    return {'date': day, 'count': count}


async def collect_plate_rank(day: str) -> dict:
    """收盘后采集原站板块级日度排行快照（14/15/17 三类）。

    数据源限流 10 req/min（模块内 6.5s 节流），三类共约 20 秒。
    某类当日无数据（如非交易日发布延迟）时跳过该类，至少一类成功即视为采集成功。
    """
    from app.datasources import zizizaizai
    result = {'date': day, 'plates': {}, 'exact': {}}
    errors = []
    for plate_type in (17, 15, 14):
        try:
            rows = await zizizaizai.plates_rank(plate_type, day, limit=500)
        except Exception as exc:  # noqa: BLE001 - one class failing must not abort the rest
            errors.append({'plate_type': plate_type, 'error': type(exc).__name__})
            continue
        if not rows:
            errors.append({'plate_type': plate_type, 'error': 'empty'})
            continue
        result['plates'][str(plate_type)] = store.plate_rank_save(plate_type, day, rows)
        # 全精度 money_leader（原站 rank/days n_days=1 口径）。失败不影响快照落库，
        # 缺 exact 的行在聚合时回退 /rank 舍入值；done 标记供断点续传回填跳过。
        marker = f'plate_rank_exact_done:{plate_type}:{day}'
        try:
            exact_rows = await zizizaizai.plates_rank_days(plate_type, day, n_days=1, limit=500)
            exact = {r['plate_code']: float(r['sum_leader_money']) for r in exact_rows
                     if r.get('plate_code') and isinstance(r.get('sum_leader_money'), (int, float))}
            result['exact'][str(plate_type)] = store.plate_rank_exact_save(plate_type, day, exact)
            store.kv_set(marker, 1)
        except Exception as exc:  # noqa: BLE001
            store.kv_set(marker, 0)
            errors.append({'plate_type': plate_type, 'step': 'exact', 'error': type(exc).__name__})
    if not result['plates']:
        raise ValueError(f'plate rank collection failed: {errors}')
    result['errors'] = errors
    return result


async def collect_plate_reasons_for(day: str, plate_types: tuple[int, ...] = (17,), top_n: int = 20) -> dict:
    from app.datasources import zizizaizai
    saved_total = 0
    errors = []
    for plate_type in plate_types:
        try:
            rows = await zizizaizai.plates_rank(plate_type, day, limit=top_n)
        except Exception as exc:  # noqa: BLE001
            errors.append({'plate_type': plate_type, 'step': 'rank', 'error': type(exc).__name__})
            continue
        for row in rows[:top_n]:
            code = row.get('plate_code')
            if not code:
                continue
            try:
                msgs = await zizizaizai.plate_popular_reason(code)
            except Exception as exc:  # noqa: BLE001
                errors.append({'plate_code': code, 'step': 'reason', 'error': type(exc).__name__})
                continue
            # 消息 date 字段为其描述的交易日（通常为前一交易日，created_at 为发布时间），
            # 直接全量落库：表按 (plate_code, trade_date, msg_id) 幂等去重。
            if msgs:
                saved_total += store.plate_reason_save(msgs)
    return {'date': day, 'saved': saved_total, 'errors': errors}


async def plate_reasons(plate_code: str, limit: int = 20) -> list[dict]:
    """板块驱动消息：本地已落库优先；本地空且开启校准源时才回源并落库。"""
    from app.config import settings
    rows = store.plate_reason_list(plate_code, limit=limit)
    if rows:
        return rows
    if not settings.enable_origin_reference:
        return []
    from app.datasources import zizizaizai
    msgs = await zizizaizai.plate_popular_reason(plate_code)
    for m in msgs:
        if not m.get("plate_code"):
            m["plate_code"] = plate_code
    if msgs:
        store.plate_reason_save(msgs)
    return msgs[:limit]


def _rank_days_from_plates(date2: str | None, n_days: int, n_type: int, limit: int, plate_type: int = 17) -> list[dict] | None:
    """板块级快照聚合（与原站数值一致）；快照不足时返回 None。"""
    # 先取完整已知日期，再按 date2 截断。只取最新 n_days+2 个日期会让较旧
    # 的历史窗口在过滤后变空（例如查询 10/15 个交易日前的矩阵列）。
    dates = store.plate_rank_dates(plate_type, limit=10000)
    if not dates:
        return None
    end = date2 or dates[0]
    dates = [d for d in dates if d <= end][:n_days]
    if len(dates) < n_days:
        return None
    if n_days > 1 and plate_type == 17:
        from app.services import plate_rank_refresh
        if any(not plate_rank_refresh.is_final(d) for d in dates):
            return None
    rows = store.plate_rank_range(plate_type, dates[-1], dates[0], with_exact=True)
    by_plate: dict[str, list[dict]] = {}
    for r in rows:
        if r.get('plate_code'):
            by_plate.setdefault(r['plate_code'], []).append(r)
    results = []
    for code, items in by_plate.items():
        items.sort(key=lambda r: r.get('date1') or '', reverse=True)
        if len(items) != n_days:
            # 不能把缺失日期隐式当作 0 再继续求和。
            continue
        if any(
            not isinstance(r.get('rate'), (int, float))
            or not isinstance(r.get('score'), (int, float))
            or not isinstance(r.get('money_leader_exact'), (int, float))
            and not isinstance(r.get('money_leader'), (int, float))
            for r in items
        ):
            continue
        # 原站多日口径（经 3/5 日双窗口对照验证）：sum_leader_money/sum_rate/sum_score
        # 均为区间各日求和（sum_rate 不是均值，sum_score 不是最新值）。
        # sum_leader_money 优先全精度（原站 rank/days 值），缺 exact 的日期回退 /rank 舍入值
        money = 0.0
        # sum_rate 用 Decimal 精确累加 + ROUND_HALF_UP。原站对第三位=5 的 tie
        # 用内部全精度浮点求和后 round，进位方向由隐藏位决定、不可逐值复刻
        # （见 docs/待解决问题清单.md 2026-09-13 条目）。实测 09-11 pt17 全量
        # 265 行：HALF_UP 残差 12（HALF_EVEN 13），且 n_type=9 默认视图
        # nd=1/2 top-20 探测 HALF_UP 零差异（HALF_EVEN 有 801273 一例 -1.945→
        # -1.94 vs 原站 -1.95），多日窗口两模式残差相同，故选 HALF_UP。
        rate_sum = Decimal('0')
        for r in items:
            v = r.get('money_leader_exact')
            money += float(v if v is not None else (r.get('money_leader') or 0))
            rate = r.get('rate')
            if isinstance(rate, (int, float)):
                rate_sum += Decimal(str(rate))
        score_sum = sum(float(r.get('score') or 0) for r in items if isinstance(r.get('score'), (int, float)))
        latest = items[0]
        results.append({
            'plate_code': code,
            'plate_name': latest.get('plate_name') or plate_name(code),
            'sum_rate': float(rate_sum.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)),
            'sum_leader_money': money,
            'sum_score': score_sum,
            'days': n_days,
            # 原站 last_day 为全零占位（plate_code "0"），实测三类(14/15/17)一致
            'last_day': {
                'plate_code': '0',
                'plate_name': '',
                'rate': 0,
                'money_leader': 0,
                'score': 0,
            },
        })
    sort_key = {3: 'sum_leader_money', 9: 'sum_score', 1: 'sum_rate'}.get(n_type, 'sum_leader_money')
    results.sort(key=lambda r: r[sort_key], reverse=True)
    return results[:limit]


async def rank_days(date2: str | None, n_days: int, data_src: int, n_type: int, limit: int = 20, plate_type: int = 17) -> list[dict]:
    """原站 /v3/market/plates/{pt}/rank/days 透明复刻（pt=17/15/14）。

    n_type: 3=按资金排序, 9=按强度排序, 1=按涨幅排序（原站行为实测）。
    n_days: 聚合交易日数 1/3/5。date2 为空取本地最新已采集日。
    只有完整的 plate_rank_daily 快照才返回结果；缺失时返回空列表，不把自定义
    个股均值/z-score 聚合伪装成同名原站 sum_rate/sum_score。
    """
    n_days = max(1, min(int(n_days or 1), 5))
    # 只使用板块级快照（数值与原站一致）；禁用无标识的个股自定义回退。
    ranked = _rank_days_from_plates(date2, n_days, n_type, limit, plate_type)
    if ranked is not None:
        return ranked
    return []


def rank_columns(date2: str | None, column_days: int, n_days: int, n_type: int, limit: int = 12, plate_type: int = 17) -> list[dict]:
    """逐日一列。最新日期在左。某一列窗口不齐就空着，不把缺日当成 0。

    column_days 仅截断「展示列数」，不删除本地更早的 plate_rank_daily。
    """
    from app.services import plate_rank_refresh
    column_days = max(1, min(int(column_days or 1), 120))
    n_days = max(1, min(int(n_days or 1), 5))
    dates = store.plate_rank_dates(plate_type, limit=10000)
    end = date2 or (dates[0] if dates else None)
    if not end:
        return []
    selected = [d for d in dates if d <= end][:column_days]
    columns = []
    for day in selected:
        status = plate_rank_refresh.day_status(day) if plate_type == 17 else "available"
        meta = plate_rank_refresh.day_meta(day) if plate_type == 17 else {}
        if n_days > 1 and status != "final":
            columns.append({
                "date": day,
                "rows": [],
                "status": "missing_input" if status == "missing_input" else status,
                "collected_at": meta.get("collected_at"),
            })
            continue
        rows = _rank_days_from_plates(day, n_days, n_type, limit, plate_type)
        if not rows:
            columns.append({
                "date": day,
                "rows": [],
                "status": "missing_input" if status == "missing_input" else status,
                "collected_at": meta.get("collected_at"),
            })
            continue
        columns.append({
            "date": day,
            "rows": rows,
            "status": status if status != "missing_input" else "available",
            "collected_at": meta.get("collected_at"),
            "source": meta.get("source"),
        })
    return columns
