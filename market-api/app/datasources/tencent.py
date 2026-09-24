"""腾讯行情客户端：实时行情 + 日K + 分时（免费、无需鉴权）。"""

from __future__ import annotations

import json

from app.datasources.codes import normalize_code, to_tencent_symbol
from app.datasources.http import fetch_json, fetch_text


REALTIME_URL = "https://qt.gtimg.cn/q={symbols}"
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
DAY_QUERY_URL = "https://web.ifzq.gtimg.cn/appstock/app/day/query"


def _parse_realtime_line(raw: str) -> dict | None:
    """解析 v_sh600000="..." 一行。"""
    if "=" not in raw:
        return None
    _, quoted = raw.split("=", 1)
    body = quoted.strip().strip('";')
    if not body:
        return None
    f = body.split("~")
    if len(f) < 6:
        return None

    def num(x: str) -> float:
        try:
            return float(x)
        except (ValueError, TypeError):
            return 0.0

    # 五档盘口: f[9..18] 买1-5, f[19..28] 卖1-5（价/量(手)交替）
    def grp(a: int) -> str:
        parts = []
        for i in range(5):
            px = num(f[a + i * 2]) if len(f) > a + i * 2 else 0.0
            vol = num(f[a + i * 2 + 1]) if len(f) > a + i * 2 + 1 else 0.0
            parts.append(f"{px:g},{int(vol)},0,")
        return "".join(parts)

    return {
        "code": normalize_code(f[2]),
        "name": f[1],
        "price": num(f[3]),
        "prev_close": num(f[4]),
        "open": num(f[5]),
        "volume": num(f[6]) if len(f) > 6 else 0.0,
        "change": num(f[31]) if len(f) > 31 else 0.0,
        "pct": num(f[32]) if len(f) > 32 else 0.0,
        "high": num(f[33]) if len(f) > 33 else 0.0,
        "low": num(f[34]) if len(f) > 34 else 0.0,
        "amount": num(f[37]) if len(f) > 37 else 0.0,  # 万元
        "turnover": num(f[38]) if len(f) > 38 else 0.0,
        "timestamp": f[30] if len(f) > 30 else "",
        "field_validity": {name: (len(f) > index and _finite_number(f[index]))
                           for name, index in {'open': 5, 'prev_close': 4, 'amount': 37, 'turnover': 38}.items()},
        "bid_grp": grp(9),
        "offer_grp": grp(19),
        "up_px": num(f[47]) if len(f) > 47 else 0.0,
        "down_px": num(f[48]) if len(f) > 48 else 0.0,
        "circulation_value": num(f[44]) * 1e8 if len(f) > 44 else 0.0,  # f[44] 单位亿
    }


def _finite_number(value: str) -> bool:
    import math
    try:
        return bool(value.strip()) and math.isfinite(float(value))
    except (ValueError, TypeError):
        return False


async def realtime(codes: list[str]) -> dict[str, dict]:
    """批量实时行情。入参为 6 位代码列表。"""
    symbols = ",".join(to_tencent_symbol(c) for c in codes)
    return await realtime_by_symbol(symbols)


async def realtime_by_symbol(symbols: str | list[str]) -> dict[str, dict]:
    """按完整 symbol（sh000001 等）批量实时行情。"""
    if isinstance(symbols, list):
        symbols = ",".join(symbols)
    r = await fetch_text(REALTIME_URL.format(symbols=symbols))
    text = r.content.decode("gbk", errors="replace")
    out: dict[str, dict] = {}
    for line in text.splitlines():
        item = _parse_realtime_line(line)
        if item:
            out[item["code"]] = item
    return out


async def kline_day_by_symbol(symbol: str, n: int = 250) -> dict:
    """按完整 symbol（如 sh000001）取日K。"""
    data = await fetch_json(KLINE_URL, params={"param": f"{symbol},day,,,{n},"})
    node = (data.get("data") or {}).get(symbol) or {}
    bars = node.get("day") or node.get("qfqday") or []
    x, y, vol = [], [], []
    prev_close = None
    for b in bars:
        # 腾讯日K: [date, open, close, high, low, volume]
        if len(b) < 6:
            continue
        x.append(b[0].replace("-", ""))
        # 原站契约 y[4]=昨收（StockKlineDay values_preclose 首选）；首根无昨收为 None，前端回退当日收盘
        y.append([float(b[1]), float(b[2]), float(b[3]), float(b[4]), prev_close])
        vol.append(int(float(b[5])))
        prev_close = float(b[2])
    return {"x": x, "y": y, "vol": vol, "code": symbol[2:]}


async def kline_day(code: str, n: int = 250) -> dict:
    """日K，返回 {'x': [...], 'y': [[o,c,h,l,v]...]} 结构与原站对齐。"""
    return await kline_day_by_symbol(to_tencent_symbol(code), n)


async def trend_minute_by_symbol(symbol: str) -> list:
    """Legacy shape [HHMM, price, cumulative VWAP, interval shares, direction]."""
    from app.datasources.intraday import parse_session
    data = await fetch_json(MINUTE_URL, params={"code":symbol})
    node = (data.get('data') or {}).get(symbol) or {}
    inner = node.get('data') or {}
    if not inner.get('data'):
        return []
    # Index prices are not a share-volume weighted stock VWAP.
    is_stock = not (symbol.startswith('sh000') or symbol.startswith('sz399') or symbol=='bj899050')
    session = parse_session(inner['date'],inner['data'],stock=is_stock)
    out, previous = [], None
    for row in session['points']:
        direction = 0 if previous is None else (1 if row['price']>previous else 2 if row['price']<previous else 0)
        out.append([row['time'].replace(':',''),row['price'],row['average_price'],row['volume'],direction])
        previous = row['price']
    return out


async def trend_minute(code: str) -> list:
    return await trend_minute_by_symbol(to_tencent_symbol(code))


async def day_query_by_symbol(symbol: str) -> dict[str, list]:
    from app.datasources.intraday import parse_session
    data = await fetch_json(DAY_QUERY_URL, params={"code":symbol})
    node = (data.get('data') or {}).get(symbol) or {}
    is_stock = not (symbol.startswith('sh000') or symbol.startswith('sz399') or symbol=='bj899050')
    out = {}
    for item in node.get('data') or []:
        session = parse_session(item['date'],item['data'],item.get('prec'),stock=is_stock)
        out[session['date'].replace('-','')] = [[r['time'].replace(':',''),r['price'],r['average_price'],r['volume']] for r in session['points']]
    return out


async def day_query(code: str) -> dict[str, list]:
    return await day_query_by_symbol(to_tencent_symbol(code))
