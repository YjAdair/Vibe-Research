"""情绪周期：由涨停家数、连板高度、炸板率推导 0-100 情绪指数。"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from app.core.cache import cache
from app.config import settings
from app.core.store import store
from app.datasources import eastmoney
from app.services import pools
from app.services.market import trade_days


TIP = (
    "温馨提示：情绪指标过高（75），短期有释放亏钱效应的风险；"
    "情绪指标过低（25），短线有反弹回暖需求；提示仅供参考"
)


def _zt_count(pool: list[dict]) -> int:
    return len(pool)


def _max_lbc(pool: list[dict]) -> int:
    return max((int(s.get("lbc") or 0) for s in pool), default=0)


def _zbc_rate(pool: list[dict]) -> float:
    """炸板率 = 炸板次数 / (涨停数 + 炸板次数)。"""
    zbc = sum(int(s.get("zbc") or 0) for s in pool)
    total = len(pool) + zbc
    return round(zbc / total, 4) if total else 0.0


def sentiment_index(zt: int, lbgd: int, zbc_rate: float) -> float:
    """0-100 情绪指数。"""
    part1 = min(zt / 80.0, 1.0) * 50.0
    part2 = min(lbgd / 7.0, 1.0) * 30.0
    part3 = (1.0 - zbc_rate) * 20.0
    return round(part1 + part2 + part3, 1)


async def _pool(date: str) -> list[dict]:
    snap = pools.published('up', date)
    if snap is not None:
        return snap['pool']
    if settings.collector_mode == 'embedded':
        try:
            return (await pools.require('up', date))['pool']
        except Exception:
            return []
    return []


async def sentiment_series(days: int = 60) -> dict:
    """近 N 个交易日的情绪序列。"""
    key = f"sentiment_series:{days}"
    hit = cache.get(key)
    if hit:
        return hit

    trading = await trade_days(days + 2)
    trading = trading[-days:]
    sem = asyncio.Semaphore(10)

    async def one(day: str) -> dict:
        async with sem:
            pool = await _pool(day)
        zt = _zt_count(pool)
        lbgd = _max_lbc(pool)
        zr = _zbc_rate(pool)
        return {
            "Day": f"{day[:4]}-{day[4:6]}-{day[6:]}",
            "ztjs": zt,
            "lbgd": lbgd,
            "zbc_rate": zr,
            "index": sentiment_index(zt, lbgd, zr),
        }

    rows = await asyncio.gather(*(one(d) for d in trading))
    result = {"rows": rows, "tip": TIP}
    cache.set(key, result, 6 * 3600)
    return result


async def sentiment_kline_day(model: int = 0, date1: str | None = None, days: int = 120) -> dict:
    """VIP /v3/api/sentiment/kline/day/{model} plus the local cycle chart payload.

    Original site returns OHLC-like {date,p_open,p_close,...}. Local bars are a
    published-snapshot proxy, not the original paid emotion candle.
    """
    series = await sentiment_series(days=max(days, 120))
    rows = series["rows"]
    if date1:
        rows = [r for r in rows if r["Day"] >= date1]
    chart = {
        "x": [r["Day"] for r in rows],
        "index": [r["index"] for r in rows],
        "ztjs": [r["ztjs"] for r in rows],
        "lbgd": [r["lbgd"] for r in rows],
        "zbc_rate": [r["zbc_rate"] for r in rows],
        "tip": series["tip"],
    }
    items = []
    prev = None
    for r in rows:
        close = float(r["index"] or 0)
        open_px = prev if prev is not None else close
        high = max(open_px, close)
        low = min(open_px, close)
        items.append({
            "id": 0,
            "modal_id": int(model or 0),
            "date": r["Day"],
            "p_open": open_px,
            "p_close": close,
            "p_high": high,
            "p_low": low,
            "p_close_pre1d": prev,
            "amount": None,
            "ztjs": r["ztjs"],
            "lbgd": r["lbgd"],
            "zbc_rate": r["zbc_rate"],
            "index": r["index"],
        })
        prev = close
    return {
        **chart,
        "items": items,
        "status": "ok" if items else "missing",
        "source": "published_limit_pools",
        "note": "local emotion-index proxy, not original paid kline/day candles",
    }


async def market_hot_day(date1: str | None = None, days: int = 180) -> dict:
    """原站 /v3/api/sentiment/market/hot/day: 按天赚钱/亏钱效应。

    本地口径来自已发布涨停/跌停池，不是原站付费模型：
    earn_effect = sentiment_index; lose_effect = 炸板率*100。
    """
    from app.services.market import trade_days
    trading = await trade_days(days + 5)
    if date1:
        start = str(date1).replace('-', '')
        if len(start) != 8 or not start.isdigit():
            return {'items': [], 'status': 'missing', 'source': 'published_limit_pools'}
        trading = [d for d in trading if d >= start]
    rows = []
    for day in trading:
        iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        up = pools.published('up', iso)
        down = pools.published('down', iso)
        if up is None:
            continue
        pool = [s for s in up.get('pool') or [] if str(s.get('c','')).startswith(('0','3','6'))]
        zt = len(pool)
        lbgd = max((int(s.get('lbc') or 0) for s in pool), default=0)
        zr = _zbc_rate(pool)
        if down is not None:
            dt = len([s for s in down.get('pool') or [] if str(s.get('c','')).startswith(('0','3','6'))])
        else:
            dt = None
        rows.append({
            'date': iso,
            'earn_effect': sentiment_index(zt, lbgd, zr),
            'lose_effect': round(zr * 100, 1),
            'lb_high': lbgd,
            'zt_num': zt,
            'dt_num': dt,
            'id': 0,
        })
    return {
        'items': rows,
        'status': 'ok' if rows else 'missing',
        'source': 'published_limit_pools',
        'note': 'earn/lose effects are local proxies from limit-up pools, not the original paid model',
    }


async def sentiment_today() -> dict:
    """当日情绪快照（对齐 /v3/open/sentiment/hot/today）。"""
    key = "sentiment_today"
    hit = cache.get(key)
    if hit:
        return hit

    today = datetime.now().strftime("%Y%m%d")
    up = pools.published('up', today)
    down = pools.published('down', today)
    stale = False
    if up is None:
        latest = pools.published('up')
        if latest:
            up = latest
            today = latest['date'].replace('-', '')
            down = pools.published('down', latest['date']) or {'total': 0, 'pool': []}
            stale = True
        elif settings.collector_mode == 'embedded':
            try:
                up = await pools.require('up', today)
                down = pools.published('down', today) or {'total': 0, 'pool': []}
            except Exception:
                up, down = {'pool': [], 'total': 0, 'date': f"{today[:4]}-{today[4:6]}-{today[6:]}"}, {'total': 0, 'pool': []}
        else:
            up, down = {'pool': [], 'total': 0, 'date': f"{today[:4]}-{today[4:6]}-{today[6:]}"}, {'total': 0, 'pool': []}
    pool = up.get('pool') or []

    zt = _zt_count(pool)
    lbgd = _max_lbc(pool)
    zr = _zbc_rate(pool)
    df_num = int((down or {}).get('total') or len((down or {}).get('pool') or []))
    info = [{
        "ztjs": str(zt),
        "Day": f"{today[:4]}-{today[4:6]}-{today[6:]}",
        "df_num": str(df_num),
        "strong": str(zt),
        "lbgd": str(lbgd),
    }]
    result = {
        "info": info,
        "tip": TIP,
        "ttag": round(time.perf_counter() % 0.01, 6),
        "errcode": "0",
        "index": sentiment_index(zt, lbgd, zr),
        "source": "published_limit_pools",
        "stale": stale,
        "pool_date": up.get('date'),
        "refresh_mode": settings.collector_mode,
    }
    cache.set(key, result, 15)
    return result
