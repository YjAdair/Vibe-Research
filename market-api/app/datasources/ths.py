"""同花顺涨停池客户端：涨停原因 + 封板统计（免费、无需鉴权）。

东财涨停池的 zttj 字段已收缩（只剩 days/ct，无涨停原因文字），
这里用同花顺 dataapi 补齐 reason_type / high_days / open_num 等字段。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.datasources.http import fetch_json

CATALOG_PATH = Path(__file__).with_name('ths_plate_catalog.json')


POOL_URL = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
FIELDS = "199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://data.10jqka.com.cn/",
}


def _high_days_lbc(high_days: str) -> int:
    """连板数：仅当 'N天N板'（天数=板数，即连续涨停）时返回 N，否则返回 1。

    high_days 的板数（如 '5天3板' 的 3）是区间涨停次数，不是连续连板数；
    下游（梯队/晋级/空间板）把 lbc 当连续连板数使用，二者不可混用。
    """
    if not high_days or "板" not in high_days:
        return 1
    try:
        head = high_days.split("板")[0]
        days = int(head.split("天")[0])
        boards = int(head.split("天")[-1])
        return boards if days == boards else 1
    except (ValueError, IndexError):
        return 1


def _ts2hm(ts: str) -> str:
    """unix 秒 -> HH:MM。"""
    import time
    try:
        return time.strftime("%H:%M", time.localtime(int(ts)))
    except (ValueError, TypeError):
        return ""


def _map(row: dict) -> dict:
    """同花顺字段 -> 统一结构（与东财涨停池对齐，补充原因字段）。"""
    return {
        "code": row.get("code", ""),
        "name": row.get("name", ""),
        "pct": row.get("change_rate", 0),
        "price": row.get("latest", 0),
        "amount": row.get("order_amount", 0),
        "lbc": _high_days_lbc(row.get("high_days", "")),
        "high_days": row.get("high_days", ""),
        "open_num": row.get("open_num") or 0,
        "reason": row.get("reason_type", ""),
        "limit_up_type": row.get("limit_up_type", ""),
        "suc_rate": row.get("limit_up_suc_rate", 0),
        "turnover_rate": row.get("turnover_rate", 0),
        "first_limit_up_time": _ts2hm(row.get("first_limit_up_time", "")),
        "last_limit_up_time": _ts2hm(row.get("last_limit_up_time", "")),
    }


async def limit_up_pool(date: str, page: int = 1, size: int = 100) -> dict:
    """涨停池（含涨停原因）。date 形如 20260909。"""
    data = await fetch_json(
        POOL_URL,
        params={
            "page": page,
            "limit": size,
            "field": FIELDS,
            "filter": "HS,GEM2STAR",
            "order_field": "330329",
            "order_type": 0,
            "date": date,
        },
        headers=HEADERS,
    )
    d = data.get("data") or {}
    page_info = d.get("page") or {}
    return {
        "total": page_info.get("total", 0),
        "pool": [_map(r) for r in d.get("info") or []],
    }


async def open_limit_pool(date: str, page: int = 1, size: int = 100) -> dict:
    """炸板池（曾涨停后打开）。date 形如 20260910。"""
    data = await fetch_json(
        POOL_URL.replace("limit_up/limit_up_pool", "limit_up/open_limit_pool"),
        params={
            "page": page,
            "limit": size,
            "field": FIELDS,
            "filter": "HS,GEM2STAR",
            "order_field": "330329",
            "order_type": 0,
            "date": date,
        },
        headers=HEADERS,
    )
    d = data.get("data") or {}
    page_info = d.get("page") or {}
    return {
        "total": page_info.get("total", 0),
        "pool": [_map(r) for r in d.get("info") or []],
    }


def _map_block_top_stock(row: dict) -> dict:
    code = str(row.get("code") or "").zfill(6)
    if len(code) != 6 or not code.isdigit():
        raise ValueError("Invalid block_top stock code")
    return {
        "code": code,
        "name": row.get("name") or "",
        "reason_info": row.get("reason_info") or "",
        "reason_type": row.get("reason_type") or "",
        "continue_num": int(row.get("continue_num") or 0),
        "high": row.get("high") or "",
        "latest": row.get("latest"),
        "change_rate": row.get("change_rate"),
        "first_limit_up_time": _ts2hm(row.get("first_limit_up_time", "")),
        "last_limit_up_time": _ts2hm(row.get("last_limit_up_time", "")),
        "change_tag": row.get("change_tag") or "",
        "market_type": row.get("market_type") or "",
    }


def _map_block_top(row: dict) -> dict:
    code = str(row.get("code") or "")
    if not code:
        raise ValueError("Invalid block_top plate code")
    stocks = [_map_block_top_stock(s) for s in (row.get("stock_list") or [])]
    codes = [s["code"] for s in stocks]
    if len(set(codes)) != len(codes):
        raise ValueError("Duplicate stock in block_top plate")
    return {
        "code": code,
        "name": row.get("name") or "",
        "change": row.get("change"),
        "limit_up_num": int(row.get("limit_up_num") or len(stocks)),
        "continuous_plate_num": int(row.get("continuous_plate_num") or 0),
        "high": row.get("high") or "",
        "days": row.get("days"),
        "stock_list": stocks,
    }


async def block_top(date: str) -> list[dict]:
    """板块涨停热度：当日涨停股最多的板块（含 reason_info 长文）。"""
    data = await fetch_json(
        "https://data.10jqka.com.cn/dataapi/limit_up/block_top",
        params={"date": date, "field": "199112"},
        headers=HEADERS,
    )
    rows = data.get("data") or []
    mapped = [_map_block_top(r) for r in rows]
    if not mapped:
        raise ValueError("Empty block_top")
    return mapped


async def continuous_limit_up(date: str) -> list[dict]:
    """连板梯队：按板高分组（height=N -> code_list），原站涨停梯队页数据源。"""
    data = await fetch_json(
        "https://data.10jqka.com.cn/dataapi/limit_up/continuous_limit_up",
        params={"date": date, "field": "199112", "filter": "HS,GEM2STAR", "page": 1, "limit": 100},
        headers=HEADERS,
    )
    return data.get("data") or []


HOT_STOCK_URL = "https://eq.10jqka.com.cn/open/api/hot_list/v1/hot_stock/a/hour/data.txt"
HOT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
    ),
    "Referer": "https://eq.10jqka.com.cn/frontend/thsTopRank",
}


def _hot_row(row: dict) -> dict:
    code = str(row.get("code") or "").zfill(6)
    if len(code) != 6 or not code.isdigit():
        raise ValueError("Invalid hot-stock code")
    rank = int(row.get("order") or 0)
    if rank <= 0:
        raise ValueError("Invalid hot-stock rank")
    tag = row.get("tag") if isinstance(row.get("tag"), dict) else {}
    rate = row.get("rate")
    try:
        heat = float(rate) if rate not in (None, "") else None
    except (TypeError, ValueError):
        heat = None
    return {
        "symbol_code": code,
        "symbol_name": row.get("name") or "",
        "rank": rank,
        "rank_diff": int(row["hot_rank_chg"]) if row.get("hot_rank_chg") is not None else None,
        "heat": heat,
        "market": row.get("market"),
        "concept_tag": list(tag.get("concept_tag") or []),
        "popularity_tag": tag.get("popularity_tag") or "",
        "analyse_title": row.get("analyse_title") or "",
    }


async def hot_stock_list(kind: str = "hour") -> dict:
    """同花顺热搜榜。hour/day 都返回当前可见 TOP 列表；公开接口不提供按历史日期回放。"""
    if kind not in ("hour", "day"):
        raise ValueError("hot list kind must be hour or day")
    url = HOT_STOCK_URL.replace("/hour/", f"/{kind}/")
    data = await fetch_json(url, headers=HOT_HEADERS)
    if str(data.get("status_code")) not in ("0", "0.0") or not data.get("data"):
        raise ValueError(data.get("status_msg") or "ths hot list failed")
    rows = ((data.get("data") or {}).get("stock_list")) or []
    if not rows:
        raise ValueError("Empty ths hot list")
    items = [_hot_row(r) for r in rows]
    ranks = [i["rank"] for i in items]
    codes = [i["symbol_code"] for i in items]
    if ranks != list(range(1, len(items) + 1)):
        raise ValueError("Hot list ranks are not a complete 1..N sequence")
    if len(set(codes)) != len(codes):
        raise ValueError("Duplicate hot-list codes")
    return {"total": len(items), "items": items, "kind": kind, "source": "10jqka_eq_hot_list"}



@lru_cache(maxsize=1)
def plate_catalog() -> dict:
    """同花顺板块代码目录：名称 -> 88xxxx/80xxxx。

    公开热股接口只给 concept_tag 文本，不给板块代码。目录来自公开人气排行里
    出现过的名称-代码对，只用于稳定映射，不在读路径打上游。
    """
    import json
    if not CATALOG_PATH.exists():
        return {'25': {}, '15': {}, '17': {}}
    data = json.loads(CATALOG_PATH.read_text(encoding='utf-8'))
    return data.get('plates') or {'25': {}, '15': {}, '17': {}}


def _alias_names(name: str) -> list[str]:
    raw = (name or '').strip()
    if not raw:
        return []
    out = [raw]
    compact = raw.replace('（', '(').replace('）', ')').replace(' ', '')
    if compact not in out:
        out.append(compact)
    wide = compact.replace('(', '（').replace(')', '）')
    if wide not in out:
        out.append(wide)
    if compact.endswith('概念'):
        stem = compact[:-2]
        if stem:
            out.append(stem)
    elif compact:
        out.append(compact + '概念')
    return out


def plate_code_for(name: str, plate_type: int = 25) -> str | None:
    plates = plate_catalog()
    order = [str(plate_type)]
    if plate_type in (15, 25):
        order = ['25', '15'] if plate_type == 25 else ['15', '25']
    aliases = _alias_names(name)
    for key in order:
        catalog = plates.get(key) or {}
        for alias in aliases:
            code = catalog.get(alias)
            if code:
                return code
    return None
