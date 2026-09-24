"""涨停梯队（涨幅区间）：对近期涨停/强势股做 N 日累计涨幅分档。"""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.core.cache import cache
from app.config import settings
from app.datasources.codes import normalize_code, market_of
from app.services import pools
from app.services.market import trade_days


TIERS = [
    ("≥100%", 100.0, float("inf")),
    ("80%-100%", 80.0, 100.0),
    ("60%-80%", 60.0, 80.0),
    ("40%-60%", 40.0, 60.0),
    ("20%-40%", 20.0, 40.0),
    ("10%-20%", 10.0, 20.0),
    ("0%-10%", 0.0, 10.0),
]

# 下跌梯队（原站 down_tiers，前端按 range 首个数字升序渲染）
DOWN_TIERS = [
    ("≤-40%", -float("inf"), -40.0),
    ("-30%~-40%", -40.0, -30.0),
    ("-20%~-30%", -30.0, -20.0),
    ("-10%~-20%", -20.0, -10.0),
    ("-5%~-10%", -10.0, -5.0),
    ("0%~-5%", -5.0, 0.0),
]

_MARKET_NAME = {"sh": "沪", "sz": "深", "bj": "北"}


async def _pool_map(days: int) -> dict[str, dict]:
    trading = await trade_days(days + 2)
    trading = trading[-days:]
    sem = asyncio.Semaphore(10)

    async def one(day: str) -> list[dict]:
        key = f"ztpool:{day}"
        hit = cache.get(key)
        if hit is not None:
            return hit
        snap = pools.published('up', day)
        if snap is None and settings.collector_mode == 'embedded':
            try:
                snap = await pools.require('up', day)
            except Exception:
                snap = None
        pool = list((snap or {}).get('pool') or [])
        cache.set(key, pool, 6 * 3600)
        return pool

    pools = await asyncio.gather(*(one(d) for d in trading))
    stock_map: dict[str, dict] = {}
    for pool in pools:
        for s in pool:
            code = normalize_code(str(s.get("c", "")))
            if code not in stock_map:
                stock_map[code] = {
                    "stock_code": code,
                    "stock_name": s.get("n", ""),
                    "market_type": _MARKET_NAME.get(market_of(code), "深"),
                    "concept": s.get("hybk") or "其他",
                    "close_price": round(float(s.get("p") or 0) / 1000, 2),
                }
    return stock_map


async def pct_tier(days: int = 10, date1: str | None = None) -> dict:
    """N 日区间累计涨幅梯队。

    优先读 daily_close 落库表（定时任务预计算架构）；
    落库数据不足时降级为涨停池口径（近 N 日出现过涨停的个股）。
    """
    key = f"pct_tier:{days}" + (":" + date1.replace("-", "") if date1 else "")
    hit = cache.get(key)
    if hit:
        return hit

    result = await _pct_tier_from_store(days, date1)
    if result is None:
        result = {
            'days': days,
            'up_tiers': [],
            'down_tiers': [],
            'status': 'missing',
            'source': 'daily_close',
            'note': '收盘快照不足，不在读路径扫描日K或涨停池',
        }
    cache.set(key, result, 1800)
    return result


async def _pct_tier_from_store(days: int, date1: str | None) -> dict | None:
    """读 daily_close 落库表计算全市场区间涨幅梯队。数据不足返回 None。"""
    from app.core.store import store

    dates = store.daily_close_dates(limit=days + 2)
    if date1:
        d1 = date1.replace("-", "")
        dates = [d for d in dates if d.replace("-", "") <= d1]
    if len(dates) < 2:
        return None
    dates = dates[:days + 1]  # 基日 + N 个交易日
    end_date, base_date = dates[0], dates[-1]
    rows = store.daily_close_range(end_date, days)
    if not rows:
        return None

    by_date: dict[str, dict[str, dict]] = {}
    for r in rows:
        if r["trade_date"] in (end_date, base_date):
            by_date.setdefault(r["trade_date"], {})[r["stock_code"]] = r
    e_map, b_map = by_date.get(end_date, {}), by_date.get(base_date, {})
    if not e_map or not b_map:
        return None

    stocks = []
    for code, e in e_map.items():
        b = b_map.get(code)
        if not b:
            continue
        c_end, c_base = e.get("close"), (b.get("prev_close") or b.get("close"))
        if not c_end or not c_base:
            continue
        stocks.append({
            "stock_code": code,
            "stock_name": e.get("stock_name") or code,
            "market_type": e.get("market_type") or "深",
            "concept": e.get("concept") or "其他",
            "pct": round((c_end - c_base) / c_base * 100, 2),
            "close_price": c_end,
        })
    return _build_tiers(stocks, days, base_date, end_date)


async def _pct_tier_from_pools(days: int) -> dict:
    """降级口径：近 N 日涨停池个股的区间累计涨幅（原实现）。"""

    stock_map = await _pool_map(days)
    sem = asyncio.Semaphore(15)

    async def calc(code: str, meta: dict) -> dict | None:
        async with sem:
            try:
                data = await kline(code, days + 5)
            except Exception:
                return None
        if len(data["y"]) < days + 1:
            return None
        closes = [bar[1] for bar in data["y"]]  # 复权收盘
        base = closes[-days - 1] if len(closes) > days else closes[0]
        if not base:
            return None
        pct = round((closes[-1] - base) / base * 100, 2)
        meta = dict(meta)
        meta["pct"] = pct
        meta["close_price"] = closes[-1]
        return meta

    results = await asyncio.gather(*(calc(c, m) for c, m in stock_map.items()))
    stocks = [r for r in results if r is not None]
    return _build_tiers(stocks, days, "", datetime.now().strftime("%Y-%m-%d"))


def _build_tiers(stocks: list[dict], days: int, base_date: str, end_date: str) -> dict:
    stocks.sort(key=lambda x: x["pct"], reverse=True)
    def build(tier_def: list) -> list[dict]:
        tiers = []
        for label, lo, hi in tier_def:
            bucket = [s for s in stocks if lo <= s["pct"] < hi or (hi == float("inf") and s["pct"] >= lo)]
            if not bucket:
                continue
            concept_groups: dict[str, dict] = {}
            for s in bucket:
                g = concept_groups.setdefault(s["concept"], {"concept": s["concept"], "count": 0, "stocks": []})
                g["count"] += 1
                g["stocks"].append(s)
            tiers.append({
                "range": label,
                "count": len(bucket),
                "stocks": bucket,
                "concept_groups": list(concept_groups.values()),
            })
        return tiers

    up_tiers = build(TIERS)
    down_tiers = build(DOWN_TIERS)
    result = {
        "date1": end_date,
        "days": days,
        "start_date": base_date,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_stocks": len(stocks),
        "up_tiers": up_tiers,
        "down_tiers": down_tiers,
    }
    return result
