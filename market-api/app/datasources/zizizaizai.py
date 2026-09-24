"""原站(quant.zizizaizai.com)公开接口的离线对照校准层。

本模块只用于离线对照已有旧数据；默认禁止一切网络访问。开发校准如确有需要，
必须显式设置 ``ZZQUANT_ENABLE_ORIGIN_REFERENCE=1``，且只在受控环境中临时开启。
接口无鉴权可读，提供 14/15/17 板块级
日度排行（score/money_leader/trade_money/rate 等完整字段），支持历史日期回放。

商业化红线：本模块仅供开发期校准，产品上线前必须替换为自有数据链路
（THS iFinD/东财 Choice 付费接口），否则存在依赖第三方站点的稳定性与合规风险。
"""
from __future__ import annotations

import asyncio

import httpx

from app.config import settings


class OriginReferenceDisabledError(RuntimeError):
    """原站对照回源未被显式开启。"""


def _require_origin_reference() -> None:
    """在任何节流、请求或其他回源副作用前拒绝默认网络访问。"""
    if not settings.enable_origin_reference:
        raise OriginReferenceDisabledError(
            'origin reference is disabled; set ZZQUANT_ENABLE_ORIGIN_REFERENCE=1 '
            'only for controlled development calibration'
        )


BASE = 'https://api.zizizaizai.com'
HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}

# 原站限流 10 req/min，节流间隔
MIN_INTERVAL = 6.5
_last_call = 0.0
_lock = asyncio.Lock()


async def plates_rank(plate_type: int, date1: str, limit: int = 500) -> list[dict]:
    """GET /v3/market/plates/{pt}/rank?date1=&limit= 板块日度排行全量。

    plate_type: 17(题材/精选), 15(概念), 14(行业)。
    返回 [{date1, plate_code, plate_name, score, money_leader, money_leader_buy,
          money_leader_sell, trade_money, market_cap_cir, rate, speed, volume_ration, time}]。
    """
    _require_origin_reference()
    global _last_call
    async with _lock:
        wait = _last_call + MIN_INTERVAL - asyncio.get_running_loop().time()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = asyncio.get_running_loop().time()
    async with httpx.AsyncClient(timeout=20, headers=HEADERS) as cli:
        r = await cli.get(f'{BASE}/v3/market/plates/{plate_type}/rank', params={'date1': date1, 'limit': limit})
        r.raise_for_status()
        d = r.json()
    if d.get('code') != 200:
        raise ValueError(f'origin plates rank error: {d.get("code")} {d.get("message") or d.get("detail")}')
    return d.get('data') or []


async def plates_rank_days(plate_type: int, date2: str, n_days: int = 1, limit: int = 500) -> list[dict]:
    """GET /v3/market/plates/{pt}/rank/days?date2=&n_days=&data_src=1&n_type=3&limit=

    多日资金聚合排行。sum_leader_money 为全精度值（/rank 的 money_leader
    是 6 位有效数字舍入值），用于回填 plate_rank_daily.money_leader_exact。
    date2 接受 YYYY-MM-DD 或 YYYYMMDD，统一转 YYYYMMDD（原站实测格式）。
    返回 [{plate_code, plate_name, sum_rate, sum_leader_money, sum_score, days, last_day}]。
    """
    _require_origin_reference()
    global _last_call
    async with _lock:
        wait = _last_call + MIN_INTERVAL - asyncio.get_running_loop().time()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = asyncio.get_running_loop().time()
    async with httpx.AsyncClient(timeout=30, headers=HEADERS) as cli:
        r = await cli.get(f'{BASE}/v3/market/plates/{plate_type}/rank/days',
                          params={'date2': date2.replace('-', ''), 'n_days': n_days,
                                  'data_src': 1, 'n_type': 3, 'limit': limit})
        r.raise_for_status()
        d = r.json()
    if d.get('code') != 200:
        raise ValueError(f'origin plates rank days error: {d.get("code")} {d.get("message") or d.get("detail")}')
    return d.get('data') or []


async def plate_popular_reason(plate_code: str) -> list[dict]:
    """GET /v3/market/plate/popular/reason?plate_code= 板块人气驱动消息列表。

    每条含 msg_id/title/is_boom/zt_num/qd(强度)/lz_info(连板梯队)/date/index_val 等，
    按日期倒序（近期驱动在前）。与板块排行共用限流节流。
    """
    _require_origin_reference()
    global _last_call
    async with _lock:
        wait = _last_call + MIN_INTERVAL - asyncio.get_running_loop().time()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = asyncio.get_running_loop().time()
    async with httpx.AsyncClient(timeout=20, headers=HEADERS) as cli:
        r = await cli.get(f'{BASE}/v3/market/plate/popular/reason', params={'plate_code': plate_code})
        r.raise_for_status()
        d = r.json()
    if d.get('code') != 200:
        raise ValueError(f'origin plate reason error: {d.get("code")} {d.get("message") or d.get("detail")}')
    return d.get('data') or []


async def sub_plates_stocks(plate_code: str, dates: list[str]) -> dict:
    """GET /v3/market/plates/17/{code}/sub-plates-stocks?dates= 子板块成分。

    返回 {sub_plates: [{code,name}], stocks: {date: {sub: [codes]}},
          stats: {date: {sub: {quote_rate, limit_up_count, limit_down_count}}}}。
    与其他接口共用限流节流（10 req/min）。
    """
    _require_origin_reference()
    global _last_call
    async with _lock:
        wait = _last_call + MIN_INTERVAL - asyncio.get_running_loop().time()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = asyncio.get_running_loop().time()
    async with httpx.AsyncClient(timeout=20, headers=HEADERS) as cli:
        r = await cli.get(f'{BASE}/v3/market/plates/17/{plate_code}/sub-plates-stocks',
                          params={'dates': ','.join(dates)})
        r.raise_for_status()
        d = r.json()
    if d.get('code') != 200:
        raise ValueError(f'origin sub plates stocks error: {d.get("code")} {d.get("message") or d.get("detail")}')
    return d.get('data') or {}


def _kline_contract(data: dict, turnover_key: str) -> dict:
    """归一为 StockKlineDay splitData 契约 {x, y:[o,c,h,l,preclose], vol, turnover}。"""
    x = data.get('x') or []
    raw = data.get(turnover_key) or []
    turnover = [float(v) for v in raw] if len(raw) == len(x) and x else None
    return {'x': x, 'y': data.get('y') or [], 'vol': data.get('vol') or [], 'turnover': turnover}


async def plate_kline_main(plate_code: str) -> dict:
    """GET /v3/open/kline/d/{code} 板块指数日K（hexin 801/803/883 系，ml 页 PlateKlineModal 数据源）。

    原站回传近 160 根：{x, y:[o,c,h,l,preclose], vol, bal(成交额), DDJE(当日净额), firstPre...}。
    bal 归一为 turnover；响应 code 为 20000。
    """
    _require_origin_reference()
    global _last_call
    async with _lock:
        wait = _last_call + MIN_INTERVAL - asyncio.get_running_loop().time()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = asyncio.get_running_loop().time()
    async with httpx.AsyncClient(timeout=20, headers=HEADERS) as cli:
        r = await cli.get(f'{BASE}/v3/open/kline/d/{plate_code}')
        r.raise_for_status()
        d = r.json()
    if d.get('code') not in (200, 20000):
        raise ValueError(f'origin plate kline error: {d.get("code")} {d.get("message") or d.get("detail")}')
    return _kline_contract(d.get('data') or {}, 'bal')


async def plate_kline_sub(plate_code: str) -> dict:
    """GET /v3/market/kline/sub-plate/{code} 二级板块日K（ml 页二级板块 StockKlineDay 数据源）。

    原站回传 {x, y:[o,c,h,l,preclose], vol, turnover(成交额)}，约 470 根；响应 code 为 20000。
    """
    _require_origin_reference()
    global _last_call
    async with _lock:
        wait = _last_call + MIN_INTERVAL - asyncio.get_running_loop().time()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = asyncio.get_running_loop().time()
    async with httpx.AsyncClient(timeout=20, headers=HEADERS) as cli:
        r = await cli.get(f'{BASE}/v3/market/kline/sub-plate/{plate_code}')
        r.raise_for_status()
        d = r.json()
    if d.get('code') not in (200, 20000):
        raise ValueError(f'origin sub plate kline error: {d.get("code")} {d.get("message") or d.get("detail")}')
    return _kline_contract(d.get('data') or {}, 'turnover')
