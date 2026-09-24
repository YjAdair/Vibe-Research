"""新浪行情客户端：日K + 实时行情（免费）。"""

from __future__ import annotations

from app.datasources.codes import normalize_code, to_sina_symbol
from app.datasources.http import fetch_json, fetch_text


KLINE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData"
REALTIME_URL = "https://hq.sinajs.cn/list={symbols}"
REALTIME_HEADERS = {"Referer": "https://finance.sina.com.cn/"}


async def kline_day(code: str, n: int = 250) -> dict:
    symbol = to_sina_symbol(code)
    rows = await fetch_json(
        KLINE_URL,
        params={"symbol": symbol, "scale": 240, "ma": "no", "datalen": n},
    )
    x, y, vol = [], [], []
    prev_close = None
    for b in rows:
        x.append(b["day"].replace("-", ""))
        o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
        # 原站契约 y[4]=昨收；首根无昨收为 None，前端 splitData 回退当日收盘
        y.append([o, c, h, l, prev_close])
        vol.append(int(float(b["volume"])))
        prev_close = c
    return {"x": x, "y": y, "vol": vol, "code": normalize_code(code)}


async def realtime(codes: list[str]) -> dict[str, dict]:
    symbols = ",".join(to_sina_symbol(c) for c in codes)
    r = await fetch_text(REALTIME_URL.format(symbols=symbols), headers=REALTIME_HEADERS)
    text = r.content.decode("gbk", errors="replace")
    out: dict[str, dict] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        sym, quoted = line.split("=", 1)
        body = quoted.strip().strip('";')
        f = body.split(",")
        if len(f) < 5 or not f[0]:
            continue
        code = sym.replace("var hq_str_", "").replace("sh", "").replace("sz", "").replace("bj", "")

        def num(x):
            try:
                return float(x)
            except (ValueError, TypeError):
                return 0.0

        out[normalize_code(code)] = {
            "code": normalize_code(code),
            "name": f[0],
            "open": num(f[1]),
            "prev_close": num(f[2]),
            "price": num(f[3]),
            "high": num(f[4]),
            "low": num(f[5]),
            "volume": num(f[8]) if len(f) > 8 else 0.0,
            "amount": num(f[9]) if len(f) > 9 else 0.0,
            "change": num(f[3]) - num(f[2]),
            "pct": round((num(f[3]) - num(f[2])) / num(f[2]) * 100, 2) if num(f[2]) else 0.0,
            "timestamp": f[30] + " " + f[31] if len(f) > 31 else "",
        }
    return out
