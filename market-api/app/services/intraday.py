"""日内情绪系统：分钟级采样 + 情绪趋势 + 涨跌分布。

对齐原站三个接口的数据结构：
- /v3/api/sentiment/trend/{0|1}  -> trend(kind) 分钟情绪序列
- /v3/sentiment/distribute/trend -> distribute_trend(date) 分钟涨跌分布
- /v3/sentiment/data             -> sentiment_data() 当日 20 字段情绪面板

数据链路：调度器每分钟拉全市场快照（东财 clist，~1s），
计算分钟横截面指标（涨跌家数/区间分布/涨停跌停/炸板），
存 SQLite minute_samples 表，接口层直接读库回放。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time as dtime
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')

from app.core.cache import cache
from app.core.store import Store
from app.config import settings
from app.datasources import eastmoney, tencent
from app.services import pools


store = Store()

# 分钟区间（对齐原站 09:30 - 15:00 共 50 根分钟样本，午后从 13:01 起）
MINUTES_AM = [f"09:{m:02d}" if m >= 30 else "" for m in range(30, 60)]  # 09:30-09:59
MINUTES_AM = [m for m in MINUTES_AM if m]
MINUTES_LATE_AM = [f"10:{m:02d}" for m in range(0, 60)] + [f"11:{m:02d}" for m in range(0, 31)]
MINUTES_PM = [f"13:{m:02d}" for m in range(0, 60)] + [f"14:{m:02d}" for m in range(0, 60)] + ["15:00"]
ALL_MINUTES = MINUTES_AM + MINUTES_LATE_AM + MINUTES_PM


def _now_minute() -> str:
    return datetime.now(TZ).strftime("%H:%M")


def _in_session(now: datetime | None = None) -> bool:
    now = now or datetime.now(TZ)
    t = now.time().replace(second=0, microsecond=0)
    return (dtime(9, 30) <= t <= dtime(11, 30)) or (dtime(13, 0) <= t <= dtime(15, 0))


def _bucket_of(pct: float) -> str | None:
    """涨跌幅分桶（对齐原站区间）。"""
    if pct > 10:
        return "gt_10_20_num"
    if pct > 7:
        return "gt_7_10_num"
    if pct > 5:
        return "gt_5_7_num"
    if pct > 2:
        return "gt_2_5_num"
    if pct > 0:
        return "gt_0_2_num"
    if pct == 0:
        return "eq_0_num"
    if pct > -2:
        return "lt_0_2_num"
    if pct > -5:
        return "lt_2_5_num"
    if pct > -7:
        return "lt_5_7_num"
    if pct > -10:
        return "lt_7_10_num"
    return "lt_10_20_num"


def _zt_price_exact(prev_close: float, code: str, name: str) -> float | None:
    """精确涨停价：先四舍五入昨收到分，再乘系数(非ST)，再半入到分。

    ST(含*ST) 不计入原站炸板口径；北交所不含(前缀过滤在快照层已完成)。
    """
    if "ST" in str(name).upper():
        return None
    p = Decimal(str(prev_close)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    coef = Decimal("1.20") if str(code).startswith(("30", "68")) else Decimal("1.10")
    return float((p * coef).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _count_zhaban(stocks: list[dict]) -> int:
    """炸板家数(原站口径)：非ST、盘中触涨停价、收盘未封住。"""
    n = 0
    for s in stocks:
        code = str(s.get("code", ""))
        if code and code[0] not in "036":
            continue
        high, price, prev = s.get("high"), s.get("price"), s.get("prev_close")
        if not all(isinstance(x, (int, float)) for x in (high, price, prev)):
            continue
        lim = _zt_price_exact(prev, code, s.get("name", ""))
        if lim is None:
            continue
        if high >= lim and price < lim:
            n += 1
    return n


def compute_minute_metrics(stocks: list[dict], pool_up: list[dict] | None = None, pool_down: list[dict] | None = None) -> dict:
    """由全市场快照计算一分钟横截面指标。

    stocks: [{code,name,pct,amount,price,high,low}]
    pool_up/pool_down: 东财涨停池/跌停池（用于封板数、炸板数精确口径）
    """
    up = down = 0
    buckets = {
        "gt_10_20_num": 0, "gt_7_10_num": 0, "gt_5_7_num": 0, "gt_2_5_num": 0,
        "gt_0_2_num": 0, "eq_0_num": 0,
        "lt_0_2_num": 0, "lt_2_5_num": 0, "lt_5_7_num": 0, "lt_7_10_num": 0, "lt_10_20_num": 0,
    }
    pcts = []
    amount_total = 0.0
    amount_missing = 0
    for s in stocks:
        # 原站口径：ST 股不计入分钟横截面统计
        if "ST" in str(s.get("name") or "").upper():
            continue
        pct = s.get("pct")
        if pct is None:
            continue
        pcts.append(pct)
        amount = s.get("amount")
        if amount is None:
            amount_missing += 1
        else:
            amount_total += amount
        if pct > 0:
            up += 1
        elif pct < 0:
            down += 1
        b = _bucket_of(pct)
        if b:
            buckets[b] += 1

    avg_pct = round(sum(pcts) / len(pcts), 2) if pcts else 0.0
    # 原站口径不含北交所（代码前缀 0/3/6）
    if pool_up is not None:
        pool_up = [s for s in pool_up if str(s.get("c", "")).startswith(("0", "3", "6"))]
    if pool_down is not None:
        pool_down = [s for s in pool_down if str(s.get("c", "")).startswith(("0", "3", "6"))]
    uplimit_num = len(pool_up) if pool_up is not None else buckets["gt_10_20_num"] + buckets["gt_7_10_num"]
    downlimit_num = len(pool_down) if pool_down is not None else buckets["lt_7_10_num"] + buckets["lt_10_20_num"]

    # fb_num 在原站已停用（长期为 0）；本地此前误将涨停家数赋给它，已对齐为 0。
    fb_num = 0
    # 原站口径(已 10/21 天精确对齐, 其余为分钟采样时点差): zb_num =
    # 全市场(非ST) 当日最高价触及涨停价 且 收盘未封住 的家数。
    # ST 股不计入(无论主板 5% 还是创/科 20%)。
    zb_num = _count_zhaban(stocks)

    return {
        "up_num": up,
        "down_num": down,
        "avg_pct": avg_pct,
        "amount": round(amount_total, 0),
        "amount_missing": amount_missing,
        "amount_complete": amount_missing == 0,
        "uplimit_num": uplimit_num,
        "downlimit_num": downlimit_num,
        "fb_num": fb_num,
        "zb_num": zb_num,
        **buckets,
    }


def reclose_minute_samples(date1: str) -> dict:
    """收盘后用已发布 daily_close 统一口径重算当日分钟样本的横截面字段。

    背景：盘中分钟样本来自实时快照（曾为主板块口径）；收盘后 daily_close
    含沪深京全市场，用它重算 up/down 与涨跌幅分桶，保证面板与原站
    "全市场去 ST"口径一致。trend/zb/涨停池等时点性字段保持采样值。
    """
    date = _iso(_compact(date1))
    rows = [r for r in store.daily_close_range(date, 1) if r.get('trade_date') == date]
    pool = []
    for r in rows:
        prev, close = r.get('prev_close'), r.get('close')
        if not prev or not close:
            continue
        if 'ST' in str(r.get('stock_name') or '').upper():
            continue
        pool.append((close - prev) / prev * 100.0)
    if not pool:
        return {'date': date, 'samples': 0, 'updated': 0}
    buckets = {
        'gt_10_20_num': 0, 'gt_7_10_num': 0, 'gt_5_7_num': 0, 'gt_2_5_num': 0,
        'gt_0_2_num': 0, 'eq_0_num': 0,
        'lt_0_2_num': 0, 'lt_2_5_num': 0, 'lt_5_7_num': 0, 'lt_7_10_num': 0, 'lt_10_20_num': 0,
    }
    up = down = 0
    for p in pool:
        if p > 0:
            up += 1
        elif p < 0:
            down += 1
        b = _bucket_of(p)
        if b:
            buckets[b] += 1
    avg_pct = round(sum(pool) / len(pool), 2)
    samples = store.minute_all(date)
    updated = 0
    for s in samples:
        s.update({
            'up_num': up, 'down_num': down, 'avg_pct': avg_pct,
            'metric_universe': 'hsj_a_exst',
            **buckets,
        })
        m = _trend_values(s)
        s['trend_main'] = m[0]
        s['trend_sensitive'] = m[1]
        store.minute_save(date, s['minute'], s)
        updated += 1
    return {'date': date, 'samples': len(samples), 'updated': updated, 'n': len(pool), 'up': up, 'down': down}


# ---- 采样器（调度器每分钟调用）----

async def sample_minute() -> dict | None:
    """Persist a minute sample only from a complete same-session snapshot."""
    if not _in_session():
        return None
    now = datetime.now(TZ)
    date = now.strftime("%Y-%m-%d")
    minute = now.strftime("%H:%M:%S")
    snap, up, down = await asyncio.gather(
        eastmoney.market_snapshot(),
        eastmoney.limit_up_pool(now.strftime("%Y%m%d")),
        eastmoney.limit_down_pool(now.strftime("%Y%m%d")),
    )
    if snap.get("trade_date") != date:
        raise ValueError("Snapshot date is not the current session")
    if not snap.get("complete") or int(snap.get("hs_count") or 0) <= 0 or int(snap.get("bj_count") or 0) <= 0:
        raise ValueError("Incomplete snapshot cannot be published as a minute sample")
    # 复用已经完整验证的全市场分钟行情给热点模块；不为每个板块重复取价。
    store.kv_set('market_quotes_current_v1', {
        'date': date, 'source_as_of': snap['source_as_of'],
        'collected_at': snap['collected_at'], 'stocks': snap['stocks'],
        'complete': True, 'source': 'eastmoney_market_snapshot'})
    # 原站口径：全市场 A 股（沪深京，去 ST 由 compute_minute_metrics 的 ST 过滤负责）
    metrics = compute_minute_metrics(snap["stocks"], up.get("pool"), down.get("pool"))
    metrics.update({
        "minute": minute,
        "date": date,
        "source_as_of": snap["source_as_of"],
        "source_as_of_max": snap["source_as_of_max"],
        "collected_at": snap["collected_at"],
        "snapshot_count": snap["total"],
        "hs_count": snap["hs_count"],
        "bj_count": snap["bj_count"],
        "metric_universe": "hsj_a_exst",
        "coverage": snap["coverage"],
        "complete": True,
    })
    m = _trend_values(metrics)
    metrics["trend_main"] = m[0]
    metrics["trend_sensitive"] = m[1]
    store.minute_save(date, minute, metrics)
    return metrics


def _trend_values(m: dict) -> tuple[float, float]:
    """由分钟横截面推导主要情绪(trend/0)与敏感情绪(trend/1)。

    主要情绪：涨跌家数比 + 封板结构（慢变量）
    敏感情绪：均价涨跌幅 + 炸板率的即时变化（快变量）
    """
    total = m.get("up_num", 0) + m.get("down_num", 0) or 1
    breadth = (m.get("up_num", 0) - m.get("down_num", 0)) / total  # -1 ~ 1
    main = round(breadth * 0.6 + (m.get("avg_pct", 0) / 3.0) * 0.4, 2)
    # 原站 fb_num 已停用，封板家数以涨停家数(uplimit_num)代替做炸板率分母
    fb = m.get("uplimit_num", 0) or 1
    zb_rate = m.get("zb_num", 0) / (fb + m.get("zb_num", 0))
    sensitive = round((m.get("avg_pct", 0) / 2.0) * 0.6 - zb_rate * 0.4, 2)
    return main, sensitive


# ---- 接口层（直接读库回放）----

async def trend(kind: int = 0, date1: str | None = None) -> list[list]:
    """原站 /v3/api/sentiment/trend/{kind}: [[HH:MM:SS, value, amount], ...]"""
    date = date1 or datetime.now(TZ).strftime("%Y-%m-%d")
    samples = store.minute_all(date)
    # 当日无样本时回退最近一个采样日(仅未指定日期时)
    if not samples and not date1:
        dates = store.minute_dates()
        if dates:
            samples = store.minute_all(dates[0])
    rows = []
    for s in samples:
        v = s.get("trend_main") if kind == 0 else s.get("trend_sensitive")
        if v is None:
            main, sens = _trend_values(s)
            v = main if kind == 0 else sens
        rows.append([s.get("minute"), v, s.get("amount", 0.0)])
    return rows


async def distribute_trend(date1: str | None = None) -> list[dict]:
    """原站 /v3/sentiment/distribute/trend: 分钟涨跌分布 50 行。"""
    date = date1 or datetime.now(TZ).strftime("%Y-%m-%d")
    samples = store.minute_all(date)
    if not samples and not date1:
        dates = store.minute_dates()
        if dates:
            samples = store.minute_all(dates[0])
    rows = []
    for s in samples:
        rows.append({
            "minute": s.get("minute"),
            "avg_pct": s.get("avg_pct", 0.0),
            "fb_num": s.get("fb_num", 0),
            "uplimit_num": s.get("uplimit_num", 0),
            "zb_num": s.get("zb_num", 0),
            "downlimit_num": s.get("downlimit_num", 0),
            "up_num": s.get("up_num", 0),
            "down_num": s.get("down_num", 0),
            "gt_10_20_num": s.get("gt_10_20_num", 0),
            "gt_7_10_num": s.get("gt_7_10_num", 0),
            "gt_5_7_num": s.get("gt_5_7_num", 0),
            "gt_2_5_num": s.get("gt_2_5_num", 0),
            "gt_0_2_num": s.get("gt_0_2_num", 0),
            "eq_0_num": s.get("eq_0_num", 0),
            "lt_0_2_num": s.get("lt_0_2_num", 0),
            "lt_2_5_num": s.get("lt_2_5_num", 0),
            "lt_5_7_num": s.get("lt_5_7_num", 0),
            "lt_7_10_num": s.get("lt_7_10_num", 0),
            "lt_10_20_num": s.get("lt_10_20_num", 0),
        })
    return rows


def _kline_n(d: str) -> int:
    """按目标日期距今天数估算需要拉的日K根数（自然日 + 余量）。

    腾讯 kline 接口按"最近 n 根"返回, 拉少了目标日不在序列里
    (此前 15 根对 21 个交易日前的日期全部漏判, 导致历史炸板数为 0/严重低估)。
    """
    try:
        days = (datetime.now(TZ) - datetime.strptime(d, "%Y%m%d")).days
    except ValueError:
        return 30
    return max(15, days + 15)


async def _zhaban_from_klines(d: str, names: dict[str, str]) -> int:
    """历史日全市场日K炸板计数(非ST、high>=涨停价、close<涨停价)。

    全市场约 5500 只, 腾讯日K单请求 ~30ms, 并发 30 时一次扫描约 6s;
    结果 kv 缓存, 之后直接命中。
    """
    codes = [c for c in (names or {}) if c[:1] in "036"]
    sem = asyncio.Semaphore(30)
    n = _kline_n(d)

    async def one(code: str) -> bool:
        async with sem:
            try:
                k = await tencent.kline_day(code, n)
            except Exception:
                return False
            if not k.get("x") or d not in k["x"]:
                return False
            i = k["x"].index(d)
            if i < 1 or len(k["y"][i]) < 4 or len(k["y"][i - 1]) < 2:
                return False
            prev_close = k["y"][i - 1][1]
            _o, c, h, _l = k["y"][i][:4]
            lim = _zt_price_exact(prev_close, code, names.get(code, ""))
            return lim is not None and h >= lim and c < lim

    results = await asyncio.gather(*(one(c) for c in codes))
    return sum(1 for r in results if r)



def _shape_counts(close_rows: list[dict], up_pool: list[dict], dt_codes: set[str]) -> dict:
    """空间板形态：只使用已发布收盘 OHLC，缺开盘/高低价则整项保持未知。"""
    needed = ('open', 'high', 'low', 'close', 'prev_close')
    rows = [r for r in close_rows if all(r.get(k) for k in needed) and str(r.get('stock_code') or '').startswith(('0','3','6'))]
    if not rows:
        return {k: None for k in ('bigleg_num','mian_num','damian_num','tiandi_num','ditian_num')}
    zt = {str(s.get('c')) for s in up_pool}
    bigleg = mian = damian = tiandi = ditian = 0
    for r in rows:
        code = str(r['stock_code'])
        o, h, l, c, prev = (float(r[k]) for k in needed)
        lim = _zt_price_exact(prev, code, r.get('stock_name') or '')
        if lim is None:
            continue
        dt = _dt_price(prev, code)
        touched = h >= lim - 0.001
        closed_limit = abs(c - lim) < 0.011
        opened_limit = o >= lim - 0.001
        body = abs(c - o)
        upper = h - max(o, c)
        lower = min(o, c) - l
        rng = h - l
        if rng <= 0:
            continue
        # 大长腿：开盘即涨停、收盘仍封住，但盘中明显打开留下影
        if closed_limit and opened_limit and lower >= max(0.05, prev * 0.02):
            bigleg += 1
        # 面：触及涨停后收盘打开，上影主导
        if touched and not closed_limit and upper >= max(body, 0.02):
            mian += 1
        # 大面：触及涨停后收盘接近或低于昨收
        if touched and c <= prev + 0.001:
            damian += 1
        # 天地板：盘中涨停又跌停
        if touched and l <= dt + 0.001:
            tiandi += 1
        # 地天板：盘中跌停又收涨停
        if l <= dt + 0.001 and closed_limit:
            ditian += 1
    return {'bigleg_num': bigleg, 'mian_num': mian, 'damian_num': damian, 'tiandi_num': tiandi, 'ditian_num': ditian}


def _dt_price(prev_close: float, code: str) -> float:
    lim = Decimal('0.2') if str(code).startswith(('30', '68')) else Decimal('0.1')
    return float((Decimal(str(prev_close)) * (Decimal('1') - lim)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

def _iso(day: str) -> str:
    d = str(day or "").replace("-", "")
    if len(d) != 8:
        return datetime.now(TZ).strftime("%Y-%m-%d")
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def _compact(day: str) -> str:
    return str(day or "").replace("-", "")


def minute_timing_value(sample: dict | None, panel: dict | None = None) -> int:
    """Local minute/day timing from published samples. Not the original paid model."""
    sample = sample or {}
    panel = panel or {}
    up = int(sample.get("up_num") or panel.get("up_num") or 0)
    down = int(sample.get("down_num") or panel.get("down_num") or 0)
    zt = int(panel.get("uplimit_num") or sample.get("uplimit_num") or 0)
    zb = sample.get("zb_num")
    if zb is None:
        zb = panel.get("zb_num") or 0
    zb = int(zb or 0)
    total = up + down
    breadth = ((up - down) / total) if total else 0.0
    zb_rate = zb / (zb + zt) if (zb + zt) else 0.0
    lbgd = int(panel.get("max_lb_num") or 0)
    score = breadth * 6.0 - zb_rate * 4.0 + min(lbgd, 8) * 0.25
    return int(max(-8, min(8, round(score))))


def timing_signal_type(samples: list[dict], panel: dict | None = None) -> int:
    """Open-signal labels only when early session samples exist; otherwise 0."""
    if not samples:
        return 0
    first = samples[0]
    minute = str(first.get("minute") or "")
    if minute[:5] > "09:40":
        return 0
    avg = first.get("avg_pct")
    if avg is None:
        return 0
    avg = float(avg)
    zb = int(first.get("zb_num") or 0)
    zt = int((panel or {}).get("uplimit_num") or first.get("uplimit_num") or 0)
    if avg >= 0.6 and zt >= 20:
        return 2
    if avg <= -0.6 or zb >= max(8, zt):
        return -2
    if minute[:5] <= "09:32" and abs(avg) >= 0.3:
        return 1 if avg > 0 else -1
    return 0


async def sentiment_data_row(date1: str) -> dict:
    """One published day of the original 20-field sentiment panel."""
    d = _compact(date1)
    date = _iso(d)

    samples = store.minute_all(date)
    last = samples[-1] if samples else {}

    # 读路径不在页面打开时计算全市场快照；没有分钟样本就保持空面板。
    up_snap = pools.published('up', date)
    down_snap = pools.published('down', date)
    if settings.collector_mode == 'embedded':
        if up_snap is None:
            try:
                up_snap = await pools.require('up', date)
            except Exception:
                up_snap = None
        if down_snap is None:
            try:
                down_snap = await pools.require('down', date)
            except Exception:
                down_snap = None
    up_pool = list((up_snap or {}).get('pool') or [])

    # 原站口径不含北交所
    up_pool = [s for s in up_pool if str(s.get("c", "")).startswith(("0", "3", "6"))]
    down_pool = [s for s in list((down_snap or {}).get('pool') or []) if str(s.get('c', '')).startswith(('0', '3', '6'))]
    dt_codes = {str(s.get('c')) for s in down_pool}

    def _count_lbc(n: int) -> int:
        return sum(1 for s in up_pool if int(s.get("lbc") or 0) == n)

    lb_counts = {int(s.get("lbc") or 1): 0 for s in up_pool}
    for s in up_pool:
        lb_counts[int(s.get("lbc") or 1)] = lb_counts.get(int(s.get("lbc") or 1), 0) + 1
    max_lb = max(lb_counts) if lb_counts else 0
    # 次高板高度：连板高度去重排序后的第二大值（如 {1:35,2:6,4:2,5:1} -> 4）
    lb_distinct = sorted(lb_counts, reverse=True)
    second_lb = lb_distinct[1] if len(lb_distinct) > 1 else 0
    max_lb_stocks = [
        {"stock_code": str(s.get("c", "")), "stock_name": s.get("n", "")}
        for s in up_pool if int(s.get("lbc") or 0) == max_lb and max_lb > 1
    ]

    # 形态类指标依赖个股日K高低价。读路径不再按请求扫描日K；未采集完成时保持未知。
    calendar = store.kv_get('collector_calendar_v1', {}) or {}
    days = [str(x).replace('-', '') for x in (calendar.get('days') or [])]
    prev_iso = days[days.index(d)-1] if d in days and days.index(d) > 0 else None
    prev_snap = pools.published('up', prev_iso) if prev_iso else None
    prev_zt = {str(item.get('c')): int(item.get('lbc') or 1) for item in (prev_snap or {}).get('pool') or []}
    store.kv_set(f"ztpool:{d}", {str(item.get("c")): int(item.get("lbc") or 1) for item in up_pool})
    broken_snap = pools.published('broken', date)
    dt_extra = 0
    close_rows = [r for r in store.daily_close_range(date, 1) if r.get('trade_date') == date]
    for r in close_rows:
        code = str(r.get('stock_code') or '')
        if code in dt_codes or not code.startswith(('0', '3', '6')):
            continue
        prev_close, price = r.get('prev_close'), r.get('close')
        if not prev_close or not price:
            continue
        if price <= _dt_price(prev_close, code) + 0.001:
            dt_extra += 1
    shapes = _shape_counts(close_rows, up_pool, dt_codes)
    bigleg, mian, damian, tiandi, ditian = (shapes[k] for k in ('bigleg_num','mian_num','damian_num','tiandi_num','ditian_num'))
    zb_num_today = last.get('zb_num')
    if zb_num_today is None and broken_snap is not None:
        zb_num_today = len([item for item in broken_snap.get('pool') or [] if str(item.get('c', '')).startswith(('0', '3', '6'))])

    row = {
        "fb_num": last.get("fb_num", 0),
        "bigleg_num": bigleg,
        "max_lb_stocks": max_lb_stocks or None,
        "date1": date,
        "uplimit_n_num": sum(1 for s in up_pool if int(s.get("lbc") or 0) == 1),
        "lt5_num": last.get("lt_5_7_num", 0) + last.get("lt_7_10_num", 0) + last.get("lt_10_20_num", 0),
        "lb_h_num": sum(1 for s in up_pool if int(s.get("lbc") or 0) >= 4),
        # 涨停家数以过滤北交所后的当日涨停池为准（与原站口径一致；分钟缓存值为盘中快照时点）
        "uplimit_num": len(up_pool) if up_pool else last.get("uplimit_num", 0),
        "up_num": last.get("up_num", 0),
        "zb_num": zb_num_today,
        "down_num": last.get("down_num", 0),
        "lb_2_num": _count_lbc(2),
        "gt5_num": last.get("gt_5_7_num", 0) + last.get("gt_7_10_num", 0) + last.get("gt_10_20_num", 0),
        "max_lb_num": max_lb,
        "downlimit_num": len(dt_codes) + dt_extra,
        "second_lb_num": second_lb,
        "lb_3_num": _count_lbc(3),
        "id": 0,
        "damian_num": damian,
        "tiandi_num": tiandi,
        "mian_num": mian,
        "ditian_num": ditian,
        "market_timing": minute_timing_value(last, {
            "up_num": last.get("up_num", 0),
            "down_num": last.get("down_num", 0),
            "uplimit_num": len(up_pool) if up_pool else last.get("uplimit_num", 0),
            "zb_num": zb_num_today,
            "max_lb_num": max_lb,
        }),
        "timing_signal_type": timing_signal_type(samples, {
            "uplimit_num": len(up_pool) if up_pool else last.get("uplimit_num", 0),
        }),
        "source": "published_limit_pools+minute_samples",
        "status": "ok" if up_snap is not None else "missing",
    }
    return row


async def sentiment_data(date1: str | None = None) -> list[dict]:
    """原站 /v3/sentiment/data: date1 起的已发布面板，缺日不回退、不现场打上游。"""
    calendar = store.kv_get("collector_calendar_v1", {}) or {}
    days = [_iso(x) for x in (calendar.get("days") or [])]
    if date1:
        start = _iso(date1)
        wanted = [d for d in days if d >= start]
        if not wanted:
            wanted = [start]
    else:
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        wanted = [today] if today in days else (days[-1:] or [today])
    rows = []
    for day in wanted:
        row = await sentiment_data_row(day)
        if row.get("status") == "missing" and not (
            row.get("uplimit_num") or row.get("up_num") or row.get("zb_num")
        ):
            continue
        rows.append(row)
    return rows


async def sentiment_timing(date1: str | None = None) -> dict:
    """原站 /v3/sentiment/timing: 逐日 {date1, market_timing, timing_signal_type}。"""
    rows = await sentiment_data(date1)
    items = [{
        "date1": r.get("date1"),
        "market_timing": r.get("market_timing"),
        "timing_signal_type": r.get("timing_signal_type") or 0,
    } for r in rows]
    return {
        "items": items,
        "status": "ok" if items else "missing",
        "source": "published_minute_samples+limit_pools",
        "note": "timing is a local breadth/limit-up model from published snapshots, not the original paid signal",
    }


async def distribute_timing(date1: str | None = None) -> dict:
    """原站 /v3/sentiment/distribute_timing: 指定日分钟择时。缺日 missing，不回退。"""
    if date1:
        date = _iso(date1)
        samples = store.minute_all(date)
        fallback = False
    else:
        date = datetime.now(TZ).strftime("%Y-%m-%d")
        samples = store.minute_all(date)
        fallback = False
        if not samples:
            dates = store.minute_dates()
            if dates:
                date = dates[0]
                samples = store.minute_all(date)
                fallback = True
    if not samples:
        return {"date": date, "items": [], "status": "missing", "source": "minute_samples"}
    panel = await sentiment_data_row(date)
    items = []
    for s in samples:
        items.append({
            "minute": s.get("minute"),
            "market_timing": minute_timing_value(s, panel),
            "up_num": s.get("up_num"),
            "down_num": s.get("down_num"),
            "avg_pct": s.get("avg_pct"),
            "uplimit_num": s.get("uplimit_num"),
            "zb_num": s.get("zb_num"),
        })
    return {
        "date": date,
        "items": items,
        "status": "ok",
        "stale": fallback,
        "source": "published_minute_samples",
        "note": "historical days often have a single close sample; missing minutes stay missing",
    }


async def sample_close() -> dict | None:
    """收盘后回补 15:00 收盘样本（若当日采样因故缺失）。"""
    now = datetime.now(TZ)
    date = now.strftime("%Y-%m-%d")
    samples = store.minute_all(date)
    if samples:
        return None
    return await sample_minute()
