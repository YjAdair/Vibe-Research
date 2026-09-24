"""市场数据接口。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi import Depends
from typing import Literal

from app.core import deps
from app.core.errors import ok
from app.datasources import ths
from app.services import boards, lhb, market, uplimit


router = APIRouter(prefix="/v3/market", tags=["market"])
legacy_router = APIRouter(prefix="/market", tags=["market"])

_EM_THS_MAP_PATH = Path(__file__).resolve().parent.parent / 'datasources' / 'em_ths_board_map.json'


@router.get("/trade/days")
async def trade_days(n: int | None = None, day_end: str | None = None, days: int | None = None):
    """交易日历。原站 ml 页契约：?day_end=YYYY-MM-DD&days=N → ISO 日期升序数组。

    - day_end 含当日（ml 页 09:31 后传明天以纳入今天）；为空取最新。
    - days/n 等价（n 为旧参数名兼容），count 上限 500。
    - 内部 market.trade_days 仍返回 YYYYMMDD（采集日历），此处统一归一为 ISO；
      前端调用点已双格式兼容。
    """
    from app.services.auction import day_iso
    try:
        end = day_iso(day_end) if day_end else None
    except ValueError as exc:
        raise HTTPException(422, '无效日期') from exc
    count = max(1, min(int(days or n or 20), 500))
    raw = await market.trade_days(500)
    iso: list[str] = []
    seen = set()
    for d in raw:
        try:
            v = day_iso(str(d))
        except ValueError:
            continue
        if v not in seen:
            seen.add(v)
            iso.append(v)
    iso.sort()
    if end:
        iso = [d for d in iso if d <= end]
    return ok(iso[-count:])


@router.get("/index/trends")
async def index_trends():
    return {"code": 200, "data": await market.index_trends()}


@router.get("/limit-up-tier")
async def limit_up_tier(date1: str | None = None):
    """连板梯队：只读已发布涨停池，按连板数分档。"""
    from datetime import datetime as _dt
    from collections import defaultdict
    d = (date1 or _dt.now().strftime("%Y%m%d")).replace("-", "")
    em = await _uplimit_pool_cached(d)
    pool = em.get("pool") or []
    by_h = defaultdict(list)
    for s in pool:
        by_h[int(s.get("lbc") or 1)].append(s)
    blocks = []
    total = 0
    def _fmt_fbt(v):
        if isinstance(v, (int, float)) and v:
            text = str(int(v)).zfill(6)
            return text[:2] + ":" + text[2:4] + ":" + text[4:]
        return v or ""
    for h in sorted(by_h, reverse=True):
        stocks = [{
            "stock_code": str(s.get("c", "")),
            "stock_name": s.get("n", ""),
            "pct": s.get("zdp", 0),
            "price": round(float(s.get("p", 0)) / 1000, 2) if s.get("p") else None,
            "lbc": s.get("lbc") or h,
            "fbt": _fmt_fbt(s.get("fbt")),
            "fund": round(float(s.get("fund") or 0) / 1e8, 2),
            "hybk": s.get("hybk", ""),
        } for s in by_h[h]]
        blocks.append({"height": h, "count": len(stocks), "stocks": stocks})
        total += len(stocks)
    status = "ok" if pool else "missing"
    return {"code": 200, "data": {"date": (em.get("date") or d).replace("-", ""), "total_stocks": total, "tiers": blocks, "source": "published_limit_pools", "status": status}}


async def _uplimit_pool_cached(d: str) -> dict:
    from app.services import pools
    from app.config import settings
    snap = pools.published('up', d)
    if snap is None and settings.collector_mode == 'embedded':
        try:
            snap = await pools.require('up', d)
        except Exception:
            snap = None
    return snap or {"total": 0, "pool": []}


@router.get("/pct-tier")
async def pct_tier(days: int = 10, date1: str | None = None):
    return {"code": 200, "data": await uplimit.pct_tier(days, date1)}


@router.get("/kline/day/batch")
async def kline_batch(codes: str, n: int = 120):
    import asyncio
    from app.services.market import kline
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    data = await asyncio.gather(*(kline(c, n) for c in code_list))
    return ok(dict(zip(code_list, data)))


@router.get("/kline/sub-plate/{code}")
async def kline_sub_plate(code: str, n: int = 250, date1: str | None = None):
    """ml 页二级板块 K 线：StockKlineDay splitData 契约 {x,y:[o,c,h,l,preclose],vol,turnover}。

    date1 仅为 URL 兼容保留（原站前端不传，序列恒为近端窗口）。
    """
    return ok(await market.plate_kline_contract(code, "sub", n))


@router.get("/kline/theme/{code}")
async def kline_theme(code: str, n: int = Query(250, ge=1, le=500), kind: str = Query("main")):
    """题材一级/二级日线统一入口：真实 OHLC 或显式 daily_nav，禁止假蜡烛。"""
    k = "sub" if str(kind).lower() in ("sub", "secondary", "18") else "main"
    return ok(await market.plate_kline_contract(code, k, n))


@router.get("/kline/plate/{code}")
async def kline_plate(code: str, n: int = 250, date1: str | None = None):
    """VIP 大盘/情绪盘：只读已发布指数或分钟样本代理，不现场打上游。"""
    data = await market.plate_kline(code, date1, n)
    return ok(data.get("items") or [])


@router.get("/trend/{code}")
async def trend(code: str):
    """原站分时契约：{preclose_px, trend:[[HH:MM, price, avg_price],...]}（ml 页 StockMiniTrend）。"""
    rows = await market.trend(code)
    quotes = await market.realtime([code])
    q = quotes.get(str(code).strip().zfill(6)) or {}
    preclose = q.get('prev_close') or 0
    out = []
    for r in rows:
        hhmm = str(r[0]).zfill(4)
        out.append([f"{hhmm[:2]}:{hhmm[2:]}", r[1], r[2]])
    return ok({"preclose_px": preclose, "trend": out})


@router.get("/trend/{code}/date/{date}")
async def trend_history(code: str, date: str):
    """历史日期分时（原站 /v3/market/trend/{code}/date/{date} 复刻）。

    读本地 stock_intraday_sessions（腾讯 day_query 多日会话，按需回补）；
    无数据返回空 trend（前端渲染占位）。
    """
    from app.config import settings
    from app.core.store import store
    from app.datasources.codes import to_tencent_symbol
    from app.services.auction import day_iso
    if len(code) != 6 or not code.isdigit():
        raise HTTPException(422, '股票代码必须是6位数字')
    try:
        day = day_iso(date)
    except ValueError as exc:
        raise HTTPException(422, '无效日期') from exc
    symbol = to_tencent_symbol(code)
    session = store.stock_intraday_get(symbol, day)
    if session is None and settings.collector_mode == 'embedded':
        from app.services import stock_intraday as si
        try:
            await si.collect(symbol)
        except Exception:
            pass
        session = store.stock_intraday_get(symbol, day)
    if not session:
        return ok({"preclose_px": 0, "trend": []})
    out = [[p.get('time'), p.get('price'), p.get('average_price')] for p in session.get('points') or []]
    return ok({"preclose_px": session.get('prev_close') or 0, "trend": out})


@router.get("/plates/{plate_type}/rank")
async def plates_rank(plate_type: int = 17, limit: int = Query(20, ge=1, le=1000), date1: str | None = None):
    """原站人气 tab 板块列表（板块级日度快照，数值与原站一致）。

    plate_type 14/15/17 优先 plate_rank_daily；无快照回退东财 boards 序列。
    原站默认按 score 倒序返回。
    """
    from app.core.store import store
    if plate_type in (14, 15, 17):
        dates = store.plate_rank_dates(plate_type, limit=1)
        if dates:
            end = date1 or dates[0]
            if end > dates[0]:
                # 未来日期无快照，回退
                pass
            else:
                rows = store.plate_rank_range(plate_type, end, end)
                if rows:
                    rows.sort(key=lambda r: float(r.get('score') or 0), reverse=True)
                    return ok(rows[:limit])
    data = await board_evolution(plate_type, date1, 1, limit, 'pct')
    columns = data['data']['columns']
    return {"code": 200, "data": columns[0]['items'] if columns else [], "meta": data['data']['snapshots']}


@router.get('/plates/{plate_type}/evolution')
async def board_evolution(plate_type: int, date1: str | None = None,
                          days: int = Query(10, ge=1, le=30),
                          limit: int = Query(20, ge=1, le=1000),
                          sort_by: Literal['pct', 'amount', 'flow'] = 'pct'):
    try:
        return ok(await boards.evolution(plate_type, date1, days, limit, sort_by))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get('/plates/{plate_type}/trend')
async def board_trend(plate_type: int, plate_code: str, day_start: str, day_end: str):
    try:
        return ok(await boards.history(plate_type, plate_code, day_start, day_end))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get('/ths/boards')
async def ths_boards(kind: str = 'all', q: str = '', limit: int = Query(100, ge=1, le=1000)):
    """THS board catalog with local coverage stats. Independent vendor series."""
    from app.core.store import store
    from app.datasources.ths_board import load_catalog
    catalog = [b for b in load_catalog() if kind in ('all', b['kind'])]
    stats = {r['board_code']: r for r in store.ths_board_daily_stats()}
    ql = q.lower()
    out = []
    for b in catalog:
        if ql and ql not in b['name'].lower() and ql not in b['code']:
            continue
        s = stats.get(b['code'])
        out.append({**b, 'bars': (s or {}).get('bars', 0), 'latest': (s or {}).get('latest')})
    return ok({'items': out[:limit], 'total': len(out), 'source': 'ths_d10jqka', 'note': 'THS board points are independent of eastmoney board points'})


@router.get('/ths/board/{code}/kline')
async def ths_board_kline(code: str, start: str = '', end: str = '', n: int = Query(140, ge=1, le=2000)):
    """THS board daily bars from local ths_board_daily store."""
    from app.core.store import store
    from app.datasources.ths_board import load_catalog
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(422, 'code must be a 6-digit THS board code')
    end = end or '2099-12-31'
    start = start or '1990-01-01'
    rows = store.ths_board_daily_range(code, start, end)
    rows = rows[-n:] if n else rows
    name = next((b['name'] for b in load_catalog() if b['code'] == code), '')
    kind = next((b['kind'] for b in load_catalog() if b['code'] == code), '')
    return ok({'code': code, 'name': name, 'kind': kind, 'bars': rows,
               'count': len(rows), 'source': 'ths_d10jqka',
               'note': 'THS board points are independent of eastmoney board points'})


@router.get('/plates/{plate_type}/{plate_code}/members')
async def board_members(plate_type: int, plate_code: str):
    import re
    if plate_type not in (1, 2, 3, 17) or not re.fullmatch(r'BK\d{4}', plate_code):
        raise HTTPException(422, 'Unsupported board identifier')
    try:
        return ok(await boards.members(plate_code))
    except Exception as exc:
        raise HTTPException(503, 'Board constituents temporarily unavailable') from exc


@router.get("/plates/{plate_type}/{plate_code}/stocks/rank")
async def plates_stocks(plate_type: int, plate_code: str, date1: str | None = None,
                        page: int = Query(1, ge=1), limit: int = Query(30, ge=1, le=300),
                        with_pct: int = 1, is_real: int = 0):
    """原站 ml 页「人气」契约：?with_pct=1&limit=&date1=&page= → {list,total}。

    - date1 给定，或 plate_code 非东财 BKxxxx（原站 801/803 板块）→ 板块成分人气榜
      （plate_flow.plate_stocks_popular_rank；plate_type 18=子板块成分，其余=全成分并集）。
    - BKxxxx 且无 date1 → 旧行为：东财板块成分列表（本地成分页使用）。
    """
    import re
    is_bk = bool(re.fullmatch(r'BK\d{4}', plate_code or ''))
    if is_bk and not date1:
        result = await board_members(plate_type, plate_code)
        return ok(result['data']['stocks'])
    from app.services import plate_flow
    from app.services.auction import day_iso
    if not plate_code or len(plate_code) > 16:
        raise HTTPException(422, '无效板块代码')
    try:
        day = day_iso(date1) if date1 else None
    except ValueError as exc:
        raise HTTPException(422, '无效日期') from exc
    if not day:
        from app.services import popular as popular_svc
        snap = popular_svc.published(None)
        day = (snap or {}).get('date')
        if not day:
            return ok({'list': [], 'total': 0, 'page': page, 'limit': limit})
    data = await plate_flow.plate_stocks_popular_rank(plate_code, day, page, limit, with_pct, plate_type)
    return ok(data)


@router.get("/lhb/list")
async def lhb_list(date1: str):
    return ok(await lhb.lhb_list(date1))


@router.get("/ths/board/map")
async def ths_board_map(plate_code: str = Query(..., min_length=4)):
    """EM 板块代码(BKxxxx) -> THS 板块代码(88xxxx) 显式映射。

    由名称匹配 + 8 日涨幅相关性审计生成（corr>=0.85），避免前端模糊匹配错配。
    """
    data = json.loads(_EM_THS_MAP_PATH.read_text(encoding='utf-8'))
    hit = data.get('mappings', {}).get(plate_code)
    if not hit:
        return ok(None)
    return ok({'ths_code': hit['ths_code'], 'em_name': hit['em_name'], 'corr': hit['corr']})

@router.get("/lhb/detail")
async def lhb_detail(date1: str, stock_code: str):
    """原站契约：{detail, traders}，traders 含买卖双方席位。"""
    return ok(lhb.stock_detail(lhb.iso(date1), stock_code))


@router.get("/lhb/trader/history")
async def lhb_trader_history(
    trader_name: str = "",
    trader_id: str = Query("", alias="trader_id"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    stock_code: str | None = None,
):
    """席位（营业部）上榜历史。优先用 trader_id（营业部代码），空则回退名称精确匹配。"""
    return ok(lhb.trader_history(trader_id, page, per_page, stock_code))


@router.get("/movement/alerts")
async def movement_alerts(date1: str | None = None, limit: int = Query(200, ge=1, le=500), type: int = 0, is_real: int = 1):
    """异动提醒：只读已发布 10/30 日偏离快照。"""
    from app.services import movement
    try:
        if date1:
            from app.services import auction
            auction.day_iso(date1)
    except ValueError as exc:
        raise HTTPException(422, '无效日期') from exc
    return ok(await movement.review(date1, limit))


@legacy_router.get("/movement/alerts")
async def movement_alerts_legacy(date1: str | None = None, limit: int = Query(200, ge=1, le=500), type: int = 0, is_real: int = 1):
    return await movement_alerts(date1, limit, type, is_real)


@legacy_router.get("/plates/17/trend")
async def plate17_trend_legacy(plate_code: str, day_start: str, day_end: str):
    """原站 /market/plates/17/trend 复刻（z tab 板块强度/成交额图数据源）。

    数据：本地 plate_rank_daily（17 板块级日度快照，字段与原站逐值一致）。
    返回按 date1 升序的板块指标序列（score/money_leader/trade_money/rate 等）。
    """
    from app.core.store import store
    from app.services.auction import day_iso
    if not plate_code or len(plate_code) > 16:
        raise HTTPException(422, '无效板块代码')
    try:
        start, end = day_iso(day_start), day_iso(day_end)
    except ValueError as exc:
        raise HTTPException(422, '无效日期') from exc
    if start > end:
        start, end = end, start
    rows = [r for r in store.plate_rank_range(17, start, end) if r.get('plate_code') == plate_code]
    rows.sort(key=lambda r: r.get('date1') or '')
    return {"code": 200, "data": rows}


@router.get("/stocks/king/rank")
async def stocks_king_rank(date2: str | None = None, date1: str | None = None, rank_down: int = 1, rank_up: int = 100, is_real: int = 1):
    from app.services import popular
    day = date2 or date1
    try:
        if day:
            from app.services import auction
            auction.day_iso(day)
    except ValueError as exc:
        raise HTTPException(422, '无效日期') from exc
    data = await popular.king_rank(day, rank_down, rank_up, is_real)
    # 原站契约：扁平 {code:20000, data:[rows]}，行 11 字段、rank 带过滤（is_real=0 精简 4 字段）
    return {'code': 20000, 'data': data.get('items') or []}

@legacy_router.get("/stocks/king/rank")
async def stocks_king_rank_legacy(date2: str | None = None, date1: str | None = None, rank_down: int = 1, rank_up: int = 100, is_real: int = 1):
    return await stocks_king_rank(date2, date1, rank_down, rank_up, is_real)


@router.get("/plates/{plate_type}/rank/popular")
async def plates_rank_popular(plate_type: int, date1: str | None = None, top_n: int = 12):
    from app.services import popular
    try:
        if date1:
            from app.services import auction
            auction.day_iso(date1)
    except ValueError as exc:
        raise HTTPException(422, '无效日期') from exc
    data = await popular.plate_popular(date1, top_n, plate_type)
    # 原站契约：扁平 {code:20000, data:[rows]}，c_num>=3 降序全量、top_n 不截断
    return {'code': 20000, 'data': data.get('items') or []}

@legacy_router.get("/plates/{plate_type}/rank/popular")
async def plates_rank_popular_legacy(plate_type: int, date1: str | None = None, top_n: int = 12):
    return await plates_rank_popular(plate_type, date1, top_n)


@router.get("/plates/{plate_type}/rank/popular/matrix")
async def plates_rank_popular_matrix(plate_type: int, date1: str | None = None, days: int = Query(6, ge=1, le=15), top_n: int = 12):
    from app.services import popular
    try:
        if date1:
            from app.services import auction
            auction.day_iso(date1)
    except ValueError as exc:
        raise HTTPException(422, '无效日期') from exc
    data = await popular.plate_popular_matrix(date1, days, top_n, plate_type)
    return ok(data)

@legacy_router.get("/plates/{plate_type}/rank/popular/matrix")
async def plates_rank_popular_matrix_legacy(plate_type: int, date1: str | None = None, days: int = Query(6, ge=1, le=15), top_n: int = 12):
    return await plates_rank_popular_matrix(plate_type, date1, days, top_n)


@router.get("/plates/{plate_type}/rank/days")
async def plates_rank_days(plate_type: int, date2: str | None = None, n_days: int = Query(1, ge=1, le=5),
                            data_src: int = Query(1, ge=1, le=2), n_type: int = Query(3),
                            limit: int = Query(20, ge=1, le=1000)):
    """易筋经 z tab（资金）：板块多日资金聚合排行。plate_type: 17(题材)/15(概念)/14(行业)。

    n_type: 3=按资金, 9=按强度, 1=按涨幅排序（实测原站行为）。
    口径：优先板块级日度快照（数值与原站一致）；快照缺失日期自动回退
    东财主力净流入×成分聚合透明口径（资金数值与原站存在系统性差异，详见 docs）。
    """
    from app.services import plate_flow
    if plate_type not in (14, 15, 17):
        raise HTTPException(422, '参数错误,可选值: 17(题材), 15(概念), 14(行业)')
    try:
        if date2:
            from app.services import auction
            auction.day_iso(date2)
    except ValueError as exc:
        raise HTTPException(422, '无效日期') from exc
    data = await plate_flow.rank_days(date2, n_days, data_src, n_type, limit=limit, plate_type=plate_type)
    # 原站响应为扁平结构 {"code":200,"data":[...]}，无附加元数据字段
    return {"code": 200, "data": data}


@router.get("/plates/{plate_type}/rank/columns")
async def plates_rank_columns(plate_type: int, date2: str | None = None, days: int = Query(10, ge=1, le=120),
                               n_days: int = Query(1, ge=1, le=5), n_type: int = Query(9),
                               limit: int = Query(12, ge=1, le=30)):
    """题材轮动横排。历史只读本地；今日未定稿时顺带刷开盘啦实时预览。

    days：展示列数（前端 5/10/15 等），上限 120；本地 plate_rank_daily 全量累积，不因本参数删库。
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.services import plate_flow, plate_rank_refresh
    if plate_type not in (14, 15, 17):
        raise HTTPException(422, '参数错误,可选值: 17(题材), 15(概念), 14(行业)')
    if n_type not in (1, 3, 9):
        raise HTTPException(422, '不支持的排序')
    try:
        if date2:
            from app.services import auction
            date2 = auction.day_iso(date2)
    except ValueError as exc:
        raise HTTPException(422, '无效日期') from exc
    if plate_type == 17:
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        end = date2 or today
        if end >= today:
            st = plate_rank_refresh.day_status(today)
            if st in ("partial_preview", "stale", "missing_input"):
                try:
                    await plate_rank_refresh.refresh_intraday(today)
                except Exception:
                    pass
    return {"code": 200, "data": plate_flow.rank_columns(date2, days, n_days, n_type, limit, plate_type)}


@router.get("/plates/{plate_type}/rank/trend")
async def plates_rank_trend(plate_type: int, plate_code: str, day_start: str, day_end: str):
    """题材轮动底部：板块强度 / 成交额。只读本地 plate_rank_daily，不回源。"""
    from app.core.store import store
    from app.services import auction
    if plate_type != 17:
        raise HTTPException(422, '当前仅支持题材 17')
    if not plate_code or len(plate_code) > 16 or not str(plate_code).isdigit():
        raise HTTPException(422, '无效板块代码')
    try:
        start, end = auction.day_iso(day_start), auction.day_iso(day_end)
    except ValueError as exc:
        raise HTTPException(422, '无效日期') from exc
    if start > end:
        start, end = end, start
    rows = [r for r in store.plate_rank_range(17, start, end) if r.get('plate_code') == plate_code]
    rows.sort(key=lambda r: r.get('date1') or '')
    return {"code": 200, "data": rows}


@router.get("/plates/{plate_type}/{plate_code}/sub-plates-stocks")
async def plates_sub_plates_stocks(plate_type: int, plate_code: str, dates: str = Query(..., min_length=8)):
    """易筋经 z tab 子板块筛选条：子板块列表 + 各日期成分 + 每日统计。

    响应结构与原站一致：{"code":200,"data":{"sub_plates":[...],"stocks":{date:{sub:[codes]}},
    "stats":{date:{sub:{quote_rate,limit_up_count,limit_down_count}}}}}。
    原站仅支持 plate_type=17（801xxx 题材板块体系）。
    """
    from app.services import plate_flow
    if plate_type != 17:
        raise HTTPException(422, '参数错误,可选值: 17(题材)')
    if not plate_code or len(plate_code) > 16:
        raise HTTPException(422, '无效板块代码')
    day_list = []
    from app.services.auction import day_iso
    for part in dates.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            day_list.append(day_iso(part))
        except ValueError as exc:
            raise HTTPException(422, '无效日期') from exc
    if not day_list:
        raise HTTPException(422, '无效日期')
    data = plate_flow.sub_plates_stocks(plate_code, day_list)
    return {"code": 200, "data": data}


@router.get("/plates/17/{plate_code}/stocks/pct")
async def plates_stocks_pct(plate_code: str, date1: str = Query(..., min_length=8), days: int = Query(10, ge=1, le=60)):
    """易筋经 z tab 累计涨幅榜：成分股 N 日累计涨幅区间分布（原站同名接口复刻）。

    cum_pct = (date1 收盘 / N 个交易日前收盘 - 1) * 100；只返回 >= 20%，
    按 [20-40, 40-60, 60-80, 80-100, 100+] 分桶、桶内降序。
    今日缺收盘日线时用实时价作期末（对齐原站盘中可出桶）。
    原站仅支持 plate_type=17（801xxx 题材板块体系）。
    """
    from app.services import plate_flow
    from app.services.auction import day_iso
    if not plate_code or len(plate_code) > 16:
        raise HTTPException(422, '无效板块代码')
    try:
        day = day_iso(date1)
    except ValueError as exc:
        raise HTTPException(422, '无效日期') from exc
    data = await plate_flow.stocks_pct_resolved(plate_code, day, days)
    return {"code": 200, "data": data}


@router.get("/plates/17/{plate_code}/stocks/pct/batch")
async def plates_stocks_pct_batch(plate_code: str, dates: str = Query(..., min_length=8),
                                  days: int = Query(10, ge=1, le=60),
                                  user: dict | None = Depends(deps.optional_user)):
    """累计涨幅榜批量版（VIP：需登录且订阅 pct_interval_vip，对齐原站 401/403 行为）。

    响应 data 即 {date: {intervals, stocks}}，与原站前端 pctDataMap 直接赋值一致。
    无订阅时每日前 3 次免费试用（原站「今日免费试用次数（3次）」机制）。
    """
    from app.services import plate_flow, user_service
    from app.services.auction import day_iso
    if not user:
        raise HTTPException(status_code=401, detail="请先登录以访问此功能")
    if not plate_code or len(plate_code) > 16:
        raise HTTPException(422, '无效板块代码')
    day_list = []
    for part in dates.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            day_list.append(day_iso(part))
        except ValueError as exc:
            raise HTTPException(422, '无效日期') from exc
    if not day_list:
        raise HTTPException(422, '无效日期')
    if not user_service.has_subscription(user["id"], "pct_interval_vip") \
            and not user_service.consume_trial(user["id"], "pct_interval_vip"):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "涨幅区间 VIP 需要订阅"})
    data = plate_flow.stocks_pct_batch(plate_code, day_list[:15], days)
    return {"code": 200, "data": data}


@router.get("/plates/18/{plate_code}/stocks/rates")
async def sub_plate_stocks_rates(plate_code: str, date1: str = Query(..., min_length=8),
                                 page: int = Query(1, ge=1, le=1000),
                                 limit: int = Query(30, ge=1, le=200),
                                 is_real: int = Query(0, ge=0, le=1)):
    """子板块个股涨幅榜。历史日读本地日线；is_real=1 时用腾讯实时刷新排序。"""
    from app.services import plate_flow
    from app.services.auction import day_iso
    if not plate_code or len(plate_code) > 16:
        raise HTTPException(422, '无效板块代码')
    try:
        day = day_iso(date1)
    except ValueError as exc:
        raise HTTPException(422, '无效日期') from exc
    data = await plate_flow.sub_plate_stocks_rates_resolved(
        plate_code, day, page, limit, is_real=bool(is_real))
    return {"code": 200, "data": data}


@router.get("/plates/17/{plate_code}/stocks/rates")
async def plate_stocks_rates(plate_code: str, date1: str = Query(..., min_length=8),
                             page: int = Query(1, ge=1, le=1000),
                             limit: int = Query(30, ge=1, le=200),
                             is_real: int = Query(0, ge=0, le=1)):
    """板块全成分个股涨幅榜。历史日读本地；is_real=1 拉实时。

    今日且本地尚无收盘日线时，即使未传 is_real 也走实时（避免空涨幅乱序）。
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.core.store import store
    from app.services import plate_flow
    from app.services.auction import day_iso
    if not plate_code or len(plate_code) > 16:
        raise HTTPException(422, '无效板块代码')
    try:
        day = day_iso(date1)
    except ValueError as exc:
        raise HTTPException(422, '无效日期') from exc
    use_real = bool(is_real)
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    if not use_real and day == today and not store.daily_close_run(day):
        use_real = True
    data = await plate_flow.plate_stocks_rates_resolved(
        plate_code, day, page, limit, is_real=use_real)
    return {"code": 200, "data": data}


@router.get("/plates/{plate_type}/{plate_code}/stocks/rank/list")
async def plate_stocks_popular_rank(plate_type: int, plate_code: str,
                                    date1: str = Query(..., min_length=8),
                                    page: int = Query(1, ge=1, le=1000),
                                    limit: int = Query(30, ge=1, le=200),
                                    with_pct: int = 0):
    """板块成分人气榜（原站 /v3/market/plates/{pt}/{code}/stocks/rank 复刻，z tab「人气」模式）。

    成分 ∩ 当日独立人气快照，rank 升序，top 300 截断；缺日不借邻日。
    路径 A：THS Top100 已采集日优先；缺日回落东财个股历史拼装（em_hist）。
    """
    from app.services import plate_flow
    from app.services.auction import day_iso
    if not plate_code or len(plate_code) > 16:
        raise HTTPException(422, '无效板块代码')
    try:
        day = day_iso(date1)
    except ValueError as exc:
        raise HTTPException(422, '无效日期') from exc
    data = await plate_flow.plate_stocks_popular_rank(plate_code, day, page, limit, with_pct, plate_type)
    return {"code": 200, "data": data}


@router.get("/plate/popular/reason")
async def plate_popular_reason(plate_code: str, limit: int = Query(20, ge=1, le=50)):
    """板块人气驱动消息（原站同名接口复刻）。

    本地 plate_reason_daily 优先；无数据时实时透传原站（开发期校准，商业化前替换）。
    """
    from app.services import plate_flow
    if not plate_code or len(plate_code) > 16:
        raise HTTPException(422, '无效板块代码')
    data = await plate_flow.plate_reasons(plate_code, limit)
    return ok(data)


@router.get("/plate/popular/reason/content")
async def plate_popular_reason_content(msgid: str = Query(..., min_length=1, max_length=32)):
    """原站 /v3/market/plate/popular/reason/content?msgid= 复刻（驱动消息正文）。

    本地 plate_reason_daily payload.content 常为 Python repr 字符串；解析后返回
    {ID, Title, Content, CreateTime(...)}。无正文时回退 boom_reason 摘要。
    """
    import ast
    from app.core.store import store
    row = store.plate_reason_get(msgid)
    if not row:
        return ok(None)
    content = row.get('content')
    parsed: dict | None = None
    if isinstance(content, dict):
        parsed = content
    elif isinstance(content, str) and content.strip():
        try:
            got = ast.literal_eval(content)
            if isinstance(got, dict):
                parsed = got
        except (ValueError, SyntaxError):
            parsed = None
    if parsed and (parsed.get('Content') or parsed.get('content') or parsed.get('Title') or parsed.get('title')):
        return ok(parsed)
    return ok({
        'ID': int(msgid) if str(msgid).isdigit() else msgid,
        'Title': row.get('title') or row.get('boom_reason') or '',
        'Content': row.get('boom_reason') or row.get('title') or '',
        'CreateTime': None,
    })
