"""东方财富结构化数据：涨停池、龙虎榜、板块（含历史回溯）。"""

from __future__ import annotations

from app.datasources.http import fetch_json


ZTPOOL_URL = "https://push2ex.eastmoney.com/getTopicZTPool"
ZTPOOL_UT = "7eea3edcaed734bea9cbfc24409ed989"
LHB_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
BOARD_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
BOARD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}


def _pool_payload(payload, requested_date):
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, dict) or type(data.get('tc')) is not int or data['tc'] < 0 or not isinstance(data.get('pool'), list):
        raise ValueError('Missing or malformed limit-pool response')
    if str(data.get('qdate', '')).replace('-', '') != str(requested_date).replace('-', ''):
        raise ValueError('Limit-pool provider date mismatch')
    if len(data['pool']) > data['tc']:
        raise ValueError('Limit-pool total mismatch')
    return {'total':data['tc'], 'qdate':str(data['qdate']), 'pool':data['pool']}


async def limit_up_pool(date: str, page: int = 0, size: int = 500) -> dict:
    """涨停池。date 形如 20260909。返回原始 data.pool。"""
    data = await fetch_json(
        ZTPOOL_URL,
        params={
            "ut": ZTPOOL_UT,
            "dpt": "wz.ztzt",
            "Pageindex": page,
            "pagesize": size,
            "sort": "fbt:asc",
            "date": date,
        },
    )
    return _pool_payload(data, date)


async def limit_broken_pool(date: str, page: int = 0, size: int = 500) -> dict:
    """炸板池（开板未回封）。date 形如 20260910，pool 内含 hybk 行业。"""
    data = await fetch_json(
        "https://push2ex.eastmoney.com/getTopicZBPool",
        params={
            "ut": ZTPOOL_UT,
            "dpt": "wz.ztzt",
            "Pageindex": page,
            "pagesize": size,
            "sort": "fbt:asc",
            "date": date,
        },
    )
    return _pool_payload(data, date)


async def limit_down_pool(date: str, page: int = 0, size: int = 500) -> dict:
    """跌停池。"""
    data = await fetch_json(
        "https://push2ex.eastmoney.com/getTopicDTPool",
        params={
            "ut": ZTPOOL_UT,
            "dpt": "wz.ztzt",
            "Pageindex": page,
            "pagesize": size,
            "sort": "fund:asc",
            "date": date,
        },
    )
    return _pool_payload(data, date)


async def lhb_seat_details(date: str, side: str, page: int = 1, size: int = 200) -> dict:
    """龙虎榜席位明细（营业部级）。side: "buy" 或 "sell"。

    reportName=RPT_BILLBOARD_DAILYDETAILSBUY / ...SELL，免费 datacenter 接口。
    """
    report = {
        "buy": "RPT_BILLBOARD_DAILYDETAILSBUY",
        "sell": "RPT_BILLBOARD_DAILYDETAILSSELL",
    }[side]
    data = await fetch_json(
        LHB_URL,
        params={
            "reportName": report,
            "columns": "ALL",
            "pageSize": size,
            "pageNumber": page,
            "sortColumns": "SECURITY_CODE",
            "sortTypes": 1,
            "filter": f"(TRADE_DATE='{date}')",
        },
    )
    result = data.get("result") or {}
    return {
        "pages": result.get("pages", 0),
        "data": result.get("data") or [],
    }


async def lhb_detail(date: str, page: int = 1, size: int = 100) -> dict:
    """龙虎榜明细。date 形如 2026-09-08。"""
    data = await fetch_json(
        LHB_URL,
        params={
            "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
            "columns": "ALL",
            "pageSize": size,
            "pageNumber": page,
            "sortColumns": "SECURITY_CODE",
            "sortTypes": 1,
            "filter": f"(TRADE_DATE='{date}')",
        },
    )
    result = data.get("result") or {}
    return {
        "pages": result.get("pages", 0),
        "data": result.get("data") or [],
    }


# ---- 板块（行业/概念）：push2delay 免费接口 ----
# fs 参数：t:2 行业板块（东财行业），t:3 概念板块，t:1 地域板块


async def board_list(board_type: int = 2, page: int = 1, size: int = 100, sort: str = "f3") -> dict:
    """板块排行。board_type: 2=行业 3=概念 1=地域。

    返回 {total, boards: [{code, name, pct, price, amount, leader_code, leader_name, up_count, down_count}]}
    """
    data = await fetch_json(
        BOARD_URL,
        params={
            "pn": page,
            "pz": size,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": sort,
            "fs": f"m:90+t:{board_type}+f:!50",
            "fields": "f2,f3,f6,f12,f14,f62,f104,f105,f124,f128,f140",
        },
        # 该主机对 httpx 的 keep-alive 连接会直接断开，必须显式 Connection: close
        headers=BOARD_HEADERS,
    )
    d = data.get("data") or {}
    boards = []
    for r in d.get("diff") or []:
        boards.append({
            "plate_code": r.get("f12", ""),
            "plate_name": r.get("f14", ""),
            "pct": r.get("f3") if isinstance(r.get("f3"), (int, float)) else None,
            "price": r.get("f2") if isinstance(r.get("f2"), (int, float)) else None,
            "amount": r.get("f6") if isinstance(r.get("f6"), (int, float)) else None,
            "main_net_inflow": r.get("f62") if isinstance(r.get("f62"), (int, float)) else None,
            "source_timestamp": r.get("f124"),
            "leader_code": r.get("f140") or "",
            "leader_name": r.get("f128") or "",
            "up_count": r.get("f104") if isinstance(r.get("f104"), (int, float)) else None,
            "down_count": r.get("f105") if isinstance(r.get("f105"), (int, float)) else None,
        })
    return {"total": d.get("total", 0), "boards": boards}


async def board_stocks(plate_code: str, page: int = 1, size: int = 100, sort: str = "f3") -> dict:
    """板块成分股。plate_code 形如 BK0475。"""
    data = await fetch_json(
        BOARD_URL,
        params={
            "pn": page,
            "pz": size,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": sort,
            "fs": f"b:{plate_code}",
            "fields": "f2,f3,f6,f12,f14,f18,f62,f66,f104,f105,f124",
        },
        headers=BOARD_HEADERS,
    )
    d = data.get("data") or {}
    stocks = []
    for r in d.get("diff") or []:
        stocks.append({
            "code": r.get("f12", ""),
            "name": r.get("f14", ""),
            "pct": r.get("f3") if isinstance(r.get("f3"), (int, float)) else None,
            "price": r.get("f2") if isinstance(r.get("f2"), (int, float)) else None,
            "prev_close": r.get("f18") if isinstance(r.get("f18"), (int, float)) else None,
            "amount": r.get("f6") if isinstance(r.get("f6"), (int, float)) else None,
            "main_net_inflow": r.get("f62") if isinstance(r.get("f62"), (int, float)) else None,
            "source_timestamp": r.get("f124"),
            "up_count": r.get("f104") if isinstance(r.get("f104"), (int, float)) else None,
            "down_count": r.get("f105") if isinstance(r.get("f105"), (int, float)) else None,
        })
    return {"total": d.get("total", 0), "stocks": stocks}


async def market_flow_snapshot() -> dict:
    """全市场个股主力净流入快照（push2delay，免费）。

    一页 100 条按 f62 降序，约 60 页；返回 {total, as_of, rows:[{code,name,pct,price,f62}]}。
    注意：delay 接口盘中数值为延迟快照，收盘后为当日终值。历史无免费源，需每日采集积累。
    """
    rows = []
    total = 0
    page = 1
    while page <= 100:
        data = await fetch_json(
            BOARD_URL,
            params={
                "pn": page,
                "pz": 100,
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f62",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
                "fields": "f2,f3,f12,f14,f62,f124",
            },
            headers=BOARD_HEADERS,
        )
        d = data.get("data") or {}
        diff = d.get("diff") or []
        if not diff:
            break
        total = d.get("total", total)
        for r in diff:
            rows.append({
                "code": r.get("f12", ""),
                "name": r.get("f14", ""),
                "pct": r.get("f3") if isinstance(r.get("f3"), (int, float)) else None,
                "price": r.get("f2") if isinstance(r.get("f2"), (int, float)) else None,
                "main_net_inflow": r.get("f62") if isinstance(r.get("f62"), (int, float)) else None,
                "source_timestamp": r.get("f124"),
            })
        if len(rows) >= total:
            break
        page += 1
    stamps = [r["source_timestamp"] for r in rows if r.get("source_timestamp")]
    as_of = min(stamps) if stamps else None
    return {"total": total, "as_of": as_of, "count": len(rows), "rows": rows}


async def stock_boards(code: str, market: int) -> dict:
    """个股所属概念板块。market: 0=深 1=沪。返回 {total, boards: [{plate_code, plate_name, pct}]}。"""
    data = await fetch_json(
        "https://push2delay.eastmoney.com/api/qt/slist/get",
        params={
            "pn": 1,
            "pz": 50,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "secid": f"{market}.{code}",
            "fields": "f3,f12,f14",
            "spt": 3,
        },
        headers=BOARD_HEADERS,
    )
    d = data.get("data") or {}
    boards = []
    for r in d.get("diff") or []:
        boards.append({
            "plate_code": r.get("f12", ""),
            "plate_name": r.get("f14", ""),
            "pct": r.get("f3") if isinstance(r.get("f3"), (int, float)) else 0.0,
        })
    return {"total": d.get("total", 0), "boards": boards}


# ---- 全市场实时快照：分钟采样器的数据基础 ----
# pz 上限 100；沪深京 A 股需分页拉全量，漏页/重复/缺时间戳批次不得发布。
SNAPSHOT_URL = BOARD_URL
HS_A_FS = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23"  # 深主板+创业板+沪主板+科创板
BJ_A_FS = "m:0 t:81 s:2048"  # 北交所 A 股；单独分页，避免与沪深页码混排
# f5=成交量(手) f26=上市日期(YYYYMMDD)：ML-R1 全市场基准与 listed_days 的点时证据。
SNAPSHOT_FIELDS = "f2,f3,f5,f6,f8,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21,f26,f100,f103,f124"
ALL_STOCKS_FS = HS_A_FS  # 兼容旧调用；完整覆盖必须走 market_snapshot()


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    from math import isfinite
    value = float(value)
    return value if isfinite(value) else None


def _row(raw: dict) -> dict | None:
    from app.datasources.codes import normalize_code, market_of
    code = normalize_code(str(raw.get("f12") or ""))
    if len(code) != 6 or not code.isdigit():
        return None
    stamp = _finite(raw.get("f124"))
    if stamp is None or stamp <= 0:
        return None
    amount = _finite(raw.get("f6"))
    return {
        "code": code,
        "name": raw.get("f14") or "",
        "pct": _finite(raw.get("f3")),
        "price": _finite(raw.get("f2")),
        "amount": amount,
        "high": _finite(raw.get("f15")),
        "low": _finite(raw.get("f16")),
        "open": _finite(raw.get("f17")),
        "prev_close": _finite(raw.get("f18")),
        "volume": _finite(raw.get("f5")),
        "list_date": str(raw.get("f26") or "").strip(),
        "turnover_ratio": _finite(raw.get("f8")),
        "vol_ratio": _finite(raw.get("f10")),
        "market_cap": _finite(raw.get("f20")),
        "circulation_value": _finite(raw.get("f21")),
        "industry": raw.get("f100") or "",
        "concepts": raw.get("f103") or "",
        "source_timestamp": int(stamp),
        "market": market_of(code),
        "secid_market": raw.get("f13"),
    }


async def _clist_universe(fs: str) -> dict:
    """One exchange-group universe. Rejects empty, truncated, duplicate or mixed-date pages."""
    import asyncio
    import math
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Shanghai")

    async def fetch(pn: int) -> dict:
        data = await fetch_json(
            SNAPSHOT_URL,
            params={
                "pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f12", "fs": fs, "fields": SNAPSHOT_FIELDS,
            },
            headers=BOARD_HEADERS,
        )
        node = data.get("data") or {}
        return {"total": int(node.get("total") or 0), "rows": node.get("diff") or []}

    first = await fetch(1)
    total = first["total"]
    if total <= 0 or not first["rows"]:
        raise ValueError("Provider returned an empty market snapshot")
    page_size = len(first["rows"])
    gate = asyncio.Semaphore(8)

    async def page(n: int) -> list[dict]:
        async with gate:
            result = await fetch(n)
            if result["total"] != total:
                raise ValueError("Provider universe changed during pagination")
            return result["rows"]

    rest = await asyncio.gather(*(page(n) for n in range(2, math.ceil(total / page_size) + 1)))
    raw_rows = first["rows"] + [r for group in rest for r in group]
    stocks = []
    seen = set()
    skipped = []
    for raw in raw_rows:
        row = _row(raw)
        if row is None:
            raise ValueError("Invalid or untimestamped snapshot row")
        if fs == BJ_A_FS and row["market"] != "bj":
            skipped.append({"code": row["code"], "name": row["name"], "reason": "not_bj_a"})
            continue
        if row["code"] in seen:
            raise ValueError("Duplicate stock in snapshot batch")
        seen.add(row["code"])
        stocks.append(row)
    if len(raw_rows) != total:
        raise ValueError("Incomplete market snapshot batch")
    if not stocks:
        raise ValueError("Provider universe contained no eligible A-share rows")
    times = [datetime.fromtimestamp(s["source_timestamp"], tz) for s in stocks]
    dates = {t.date().isoformat() for t in times}
    if len(dates) != 1:
        raise ValueError("Mixed provider trading dates in snapshot")
    now = datetime.now(tz)
    trade_date = next(iter(dates))
    if trade_date > now.date().isoformat() or max(times) > now:
        raise ValueError("Snapshot timestamp is in the future")
    session_times = [t for t in times if t.time().hour >= 9]
    return {
        "trade_date": trade_date,
        "source_as_of": min(session_times or times).isoformat(),
        "source_as_of_max": max(times).isoformat(),
        "preopen_timestamp_count": sum(1 for t in times if t.time().hour < 9),
        "stocks": stocks,
        "total": len(stocks),
        "provider_total": total,
        "skipped": skipped,
        "fs": fs,
    }


async def market_snapshot() -> dict:
    """沪深京 A 股完整批次。漏页、重复或缺时间戳时抛错，不发布部分结果。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    hs, bj = await _clist_universe(HS_A_FS), await _clist_universe(BJ_A_FS)
    if hs["trade_date"] != bj["trade_date"]:
        raise ValueError("沪深与北交所快照交易日不一致")
    stocks = hs["stocks"] + bj["stocks"]
    if len({s["code"] for s in stocks}) != len(stocks):
        raise ValueError("Duplicate stock across exchange groups")
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    priced = [s for s in stocks if s.get("price") and s.get("prev_close") and s["price"] > 0 and s["prev_close"] > 0]
    return {
        "trade_date": hs["trade_date"],
        "source": "eastmoney_clist",
        "source_as_of": min(hs["source_as_of"], bj["source_as_of"]),
        "source_as_of_max": max(hs["source_as_of_max"], bj["source_as_of_max"]),
        "collected_at": now.isoformat(),
        "hs_count": hs["total"],
        "bj_count": bj["total"],
        "provider_hs_total": hs["provider_total"],
        "provider_bj_total": bj["provider_total"],
        "skipped_non_a": hs["skipped"] + bj["skipped"],
        "preopen_timestamp_count": hs["preopen_timestamp_count"] + bj["preopen_timestamp_count"],
        "unpriced_count": len(stocks) - len(priced),
        "priced_count": len(priced),
        "total": len(stocks),
        "complete": True,
        "coverage": "hsj_a",
        "stocks": stocks,
        "amount_unit": "yuan",
        "amount_basis": "eastmoney f6 cumulative turnover; missing stays null",
        "price_basis": "unpriced rows are kept as suspended/delisted observations and excluded from close publication",
    }


# ---- 股吧人气榜（原站 stocks_hot 的数据源）----
POPULARITY_URL = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
POPULARITY_HIS_URL = "https://emappdata.eastmoney.com/stockrank/getHisList"
SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"


async def search_suggest(keyword: str, count: int = 10) -> list[dict]:
    """名称/拼音/代码模糊搜索（东财 suggest）。返回 [{code, name, market}, ...]。"""
    data = await fetch_json(
        SUGGEST_URL,
        params={
            "input": keyword,
            "type": 14,
            "token": "D43BF722C8E33BDC906FB84D85E326E8",
            "count": count,
        },
        headers={"User-Agent": "Mozilla/5.0"},
    )
    rows = ((data.get("QuotationCodeTable") or {}).get("Data")) or []
    out = []
    for r in rows:
        code = str(r.get("Code", "")).zfill(6)
        # 只保留 A 股（沪 60/68、深 00/30、北 43/83/87/88/92）
        if not (len(code) == 6 and code.isdigit() and code[:2] in ("60", "68", "00", "30", "43", "83", "87", "88", "92")):
            continue
        out.append({
            "code": code,
            "name": r.get("Name", ""),
            "market": int(r.get("MktNum") or 0),
        })
    return out


async def popularity_rank(size: int = 100) -> list[dict]:
    """股吧人气榜 TOP N。返回 [{code, rank, rank_change}, ...]（按排名升序）。"""
    body = {
        "appId": "appId01",
        "globalId": "786e4c21-70dc-435a-93bb-38",
        "marketType": "",
        "pageNo": 1,
        "pageSize": min(size, 100),
    }
    data = await fetch_json(
        POPULARITY_URL,
        method="POST",
        json=body,
        headers={"Content-Type": "application/json"},
    )
    rows = data.get("data") or []
    out = []
    for r in rows:
        sc = str(r.get("sc", ""))
        code = sc[2:] if sc[:2] in ("SH", "SZ", "BJ") else sc
        out.append({
            "code": code,
            "rank": int(r.get("rk") or 0),
            "rank_change": int(r.get("rc") or 0),
            "history_change": int(r.get("hisRc") or 0),
        })
    return out


async def popularity_rank_history(code: str, year_type: str = "2") -> list[dict]:
    """个股东财股吧人气日序。返回 [{trade_date, rank}, ...]（升序）。

    year_type=2 约覆盖近一年交易日；接口无按日全市场回放，只能按标的拉。
    """
    from app.datasources.codes import normalize_code, to_eastmoney_rank_sc
    body = {
        "appId": "appId01",
        "globalId": "786e4c21-70dc-435a-93bb-38",
        "marketType": "",
        "srcSecurityCode": to_eastmoney_rank_sc(code),
        "yearType": str(year_type),
    }
    data = await fetch_json(
        POPULARITY_HIS_URL,
        method="POST",
        json=body,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    rows = data.get("data") or []
    out = []
    for r in rows:
        day = str(r.get("calcTime") or "")[:10]
        if len(day) != 10 or day[4] != "-":
            continue
        try:
            rk = int(r.get("rank"))
        except (TypeError, ValueError):
            continue
        if rk <= 0:
            continue
        out.append({"trade_date": day, "rank": rk, "symbol_code": normalize_code(code)})
    out.sort(key=lambda x: x["trade_date"])
    return out


KLINE_URL = "https://92.push2his.eastmoney.com/api/qt/stock/kline/get"
KLINE_URL_FALLBACKS = [
    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://push2delay.eastmoney.com/api/qt/stock/kline/get",
]

_kline_last_ts = 0.0


async def _kline_fetch(params: dict, headers: dict) -> dict:
    """带主机重试和全局限频的 K 线请求。东财对高频请求会断连或返回空 klines。"""
    import asyncio
    import time

    global _kline_last_ts
    last_exc: Exception | None = None
    for attempt in range(4):
        # 全局至少间隔 0.35s，避免触发限频
        wait = _kline_last_ts + 0.35 - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _kline_last_ts = time.monotonic()
        for url in [KLINE_URL, *KLINE_URL_FALLBACKS]:
            try:
                data = await fetch_json(url, params=params, headers=headers)
                if (data.get("data") or {}).get("klines"):
                    return data
            except Exception as exc:  # noqa: BLE001 - 逐主机重试
                last_exc = exc
                continue
        await asyncio.sleep(0.8 * (attempt + 1))
    if last_exc:
        raise last_exc
    return {}


async def kline_history(code: str, n_days: int = 60, *, fqt: int = 0,
                        beg: str | None = None, end: str = "20500101") -> list[dict]:
    """东财日 K 历史。fqt=0 不复权 / fqt=2 后复权（总收益因子证据）。
    每根含 date/open/close/high/low/volume(手)/amount/turnover_rate(%)；
    change(f60) 为当日官方涨跌额：close-change 即官方昨收（除权日=除权参考价）。

    用于给已发布 daily_close 补 turnover_ratio；流通市值可由 close*volume/turnover 反推。
    """
    from app.datasources.codes import to_eastmoney_secid

    params = {
        "secid": to_eastmoney_secid(code),
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": str(fqt),
        "end": end,
    }
    if beg:
        params["beg"] = beg
    else:
        params["lmt"] = str(n_days)
    # 这些主机对 httpx 的 keep-alive 连接会直接断开，必须显式 Connection: close；
    # 且负载均衡后端不稳定，逐个主机重试。
    headers = {**BOARD_HEADERS, "Connection": "close"}
    data = await _kline_fetch(params, headers)
    klines = ((data.get("data") or {}).get("klines")) or []
    out = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 11:
            continue
        try:
            out.append({
                "date": parts[0],
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
                "amount": float(parts[6]),
                "amplitude": float(parts[7]),
                "pct_chg": float(parts[8]),
                "change": float(parts[9]),
                "turnover_rate": float(parts[10]),
            })
        except (ValueError, IndexError):
            continue
    return out
