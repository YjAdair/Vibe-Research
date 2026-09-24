"""公开数据接口（无需鉴权）。"""

from __future__ import annotations

import re
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from app.core.errors import ok, fail, PARAM_ERROR
from app.core.store import store
from app.datasources import eastmoney, ths
from app.services import market, sentiment, pools


router = APIRouter(prefix="/v3/open", tags=["open"])

_THS_BOARD_RE = re.compile(r'^(?:801|803)\d{3}$')


def _uplimit_hot_board(board: str, day: str, pool_d: str | None = None) -> dict:
    """原站 /review/uplimit/hot?board=801xxx 矩阵形态（ml 页涨停梯队）。

    本地清洗：公开涨停/炸板池 ∩ 板块全量成分，再用 daily_close 收盘价校验封板；
    池外收盘封板补入；池内收盘未封改炸。详细口径见 ladder_clean / docs。
    score/plate_name 取 plate_rank_daily(17)。
    """
    from app.services import plate_flow
    from app.services.ladder_clean import build_ladder_rows

    score, pname = plate_flow.plate_rank_score(board, day)
    score = score if score is not None else 0
    members = plate_flow.plate_members_full(board, day)
    built = build_ladder_rows(day, members)
    sealed = [{**{k: v for k, v in r.items() if not k.startswith('_')}, 'plate_code': board}
              for r in built['sealed']]
    broken = [{**{k: v for k, v in r.items() if not k.startswith('_')}, 'plate_code': board}
              for r in built['broken']]
    stocks = sealed + broken

    max_count = max((x['up_limit_keep_times'] for x in sealed), default=0)
    ban_info = {str(k): {'count': sum(1 for x in sealed if x['up_limit_keep_times'] == k)}
                for k in range(1, max_count + 1)}
    codes = [x['stock_code'] for x in stocks]
    zb_items = [{
        'stock_code': x['stock_code'], 'stock_name': x['stock_name'],
        'up_limit_type': x['up_limit_type'], 'up_limit_time': x['up_limit_time'],
        'up_limit_desc': x['up_limit_desc'], 'up_limit_keep_times': 0,
        'plate_code': board,
    } for x in broken]
    pool_source = built['pool_source']
    status = 'ok' if pool_source or sealed or broken else 'missing'
    clean = built['meta']
    note_parts = []
    if not pool_source and not (sealed or broken):
        note_parts.append('缺少该日涨停/炸板池快照')
    if clean.get('supplemented_close_seal'):
        note_parts.append(f"收盘封板补漏{len(clean['supplemented_close_seal'])}只")
    if clean.get('reclassified_to_broken'):
        note_parts.append(f"收盘未封改炸{len(clean['reclassified_to_broken'])}只")
    return {
        'today': day == datetime.now().date().isoformat(),
        'plate': [[pname, board, score]],
        'plate_info': {board: {'score': score, 'name': pname, 'code': board}},
        'plate_stocks': {board: stocks},
        'plate_stocks_bx': {board: []},
        'plate_stocks_next_yi': {},
        'plate_stocks_zb': {board: zb_items},
        'ban_info': ban_info,
        'max_count': max_count,
        'stocks': ','.join(codes),
        'stock_info': {c: {'plates': [pname]} for c in codes},
        'stocks_hot': {c: 1 for c in codes},
        'stocks_hot_n': 0,
        'relay': {'area': [{'p_code': board, 'p_score': score,
                            'count': ban_info[str(k)]['count'], 'ban_n': k}
                           for k in range(1, max_count + 1)]},
        'status': status,
        'source': f'ladder_clean:{pool_source or "close_seal"}',
        'meta': {
            'status': status,
            'pool_source': pool_source,
            'clean': clean,
            'note': '；'.join(note_parts),
        },
    }


def _derive_board_from_snapshot(snap: dict, board: str, day: str) -> dict:
    """从全市场梯队矩阵快照（review/uplimit/hot 不带 board）推导 board=X 过滤响应。

    过滤语义对 2026-09-11 原站 801660 完整响应逐字段对拍验证、801807 验证空模板：
    - 跟踪板块集合 = 快照 plate_stocks 的键集；集合外板块返回空模板
      （原站 board 解析是复盘系统当日跟踪集合，而非成分∩涨停池）
    - plate_stocks / plate_stocks_zb 直接取快照中该板块的行（含炸板行 kt=0）
    - stocks csv = 全市场 csv 的保序子集（只留本板块行内代码，≠行序）
    - ban_info = keep_times 各档 1..max_count 计数（含 0 档）；
      max_count = max(keep_times, 1)
    - relay.area 每档一条：ban_n=1 的 count 为 1 档计数；ban_n>=2 的 count 为
      desc=="{n}连板" 的真连板数（排除 "N天M板"）
    - stock_info={code:{plates:[板块名]}}；stocks_hot={code:1}
    - stocks_hot_n = 板块行内全市场人气(stocks_hot)>=20 的股票数
    - 键序与原站响应一致（today/plate/.../ban_info），序列化逐字节对齐
    """
    today = day == datetime.now().date().isoformat()
    ps_all = snap.get('plate_stocks') or {}
    if board not in ps_all:
        return {
            'today': today, 'plate': [], 'plate_info': {}, 'plate_stocks': {},
            'plate_stocks_zb': {}, 'plate_stocks_bx': {}, 'stocks': '',
            'max_count': 1, 'relay': {'area': []}, 'plate_stocks_next_yi': {},
            'stock_info': {}, 'stocks_hot': {}, 'stocks_hot_n': [],
            'ban_info': {'1': {'count': 0}},
        }
    rows = ps_all[board] or []
    zb = (snap.get('plate_stocks_zb') or {}).get(board) or []
    info = (snap.get('plate_info') or {}).get(board) or {'score': 0, 'name': '', 'code': board}
    pname = info.get('name') or ''
    score = info.get('score') or 0
    row_codes = {r.get('stock_code') for r in rows}
    csv = [c for c in (snap.get('stocks') or '').split(',') if c and c in row_codes]
    row_order = [r.get('stock_code') for r in rows]  # stock_info/stocks_hot 键序=行序（原站）
    sealed = [r for r in rows if (r.get('up_limit_keep_times') or 0) > 0]
    max_count = max(((r.get('up_limit_keep_times') or 0) for r in sealed), default=0)
    max_count = max(max_count, 1)
    ban_info = {str(k): {'count': sum(1 for r in sealed if (r.get('up_limit_keep_times') or 0) == k)}
                for k in range(1, max_count + 1)}
    area = []
    for k in range(1, max_count + 1):
        if k == 1:
            cnt = ban_info['1']['count']
        else:
            cnt = sum(1 for r in sealed if r.get('up_limit_desc') == f'{k}连板')
        area.append({'p_code': board, 'p_score': score, 'count': cnt, 'ban_n': k})
    hot = snap.get('stocks_hot') or {}
    hot_n = sum(1 for r in rows if (hot.get(r.get('stock_code')) or 0) >= 20)
    return {
        'today': today,
        'plate': [[pname, board, score]],
        'plate_info': {board: {'score': score, 'name': pname, 'code': board}},
        'plate_stocks': {board: rows},
        'plate_stocks_zb': {board: zb},
        'plate_stocks_bx': {board: []},
        'stocks': ','.join(csv),
        'max_count': max_count,
        'relay': {'area': area},
        'plate_stocks_next_yi': {},
        'stock_info': {c: {'plates': [pname]} for c in row_order},
        'stocks_hot': {c: 1 for c in row_order},
        'stocks_hot_n': hot_n,
        'ban_info': ban_info,
    }


def _build_real_payload(data: dict) -> dict:
    """原站 fields/values 数组结构（ml 页涨停梯队实时叠加 + 复盘页增量刷新共用）。"""
    fields = list(data.keys())
    values = []
    for c in fields:
        v = data.get(c) or {}
        up_px = v.get("up_px") or 0.0
        last_px = v.get("price") or 0.0
        values.append({
            "prod_name": v.get("name", ""),
            "last_px": last_px,
            "offer_grp": v.get("offer_grp", ""),
            "bid_grp": v.get("bid_grp", ""),
            "preclose_px": v.get("prev_close"),
            "px_change_rate": v.get("pct"),
            "open_px": v.get("open"),
            "high_px": v.get("high"),
            "low_px": v.get("low"),
            "down_px": v.get("down_px"),
            "up_px": up_px or None,
            "circulation_value": v.get("circulation_value"),
            "turnover_ratio": v.get("turnover"),
            "business_amount": (v.get("amount") or 0) * 1e4,
            "symbol": c,
            "is_up_limit": bool(up_px and abs(last_px - up_px) < 0.005),
        })
    return {"fields": fields, "values": values}


@router.get("/market/real")
async def market_real(codes: str | None = None, symbols: str | None = None):
    """实时行情。codes/symbols 均可（原站前端用 symbols）。

    原站 ml 页契约：data = {fields:[代码...], values:[行情对象...]}，
    values[i] 与 fields[i] 对应（涨停梯队 real_time_info_stock 按 indexOf 取值）。
    """
    raw = codes or symbols
    if not raw:
        return fail(PARAM_ERROR, data="参数错误")
    code_list = [c.strip() for c in raw.split(",") if c.strip()]
    data = await market.realtime(code_list)
    return ok(_build_real_payload(data))


@router.get("/market/real/batch")
async def market_real_batch(symbols: str):
    """批量实时行情（对齐原站 fields/values 数组结构，复盘页增量刷新用）。"""
    code_list = [c.strip() for c in symbols.split(",") if c.strip()]
    data = await market.realtime(code_list)
    return ok(_build_real_payload(data))


@router.get("/sentiment/hot/today")
async def sentiment_hot_today():
    return ok(await sentiment.sentiment_today())


@router.get("/kline/d/{code}")
async def open_kline(code: str, n: int = 250):
    return ok(await market.kline(code, n))


@router.get("/stock/{code}")
async def open_stock(code: str):
    data = await market.realtime([code])
    return ok(data.get(code.strip().zfill(6), {}))


@router.get("/review/dingpan/auction")
async def dingpan_auction(date1: str | None = None, date: str | None = None,
                         days: int = Query(3, ge=1, le=10)):
    """Date-specific auction read model with provenance and field coverage."""
    from app.services import auction
    try:
        selected = auction.day_iso(date1 or date) if date1 or date else None
        if date1 and date and auction.day_iso(date1) != auction.day_iso(date):
            raise ValueError('Conflicting date aliases')
        if selected and selected > auction.now_local().date().isoformat():
            raise ValueError('Future date')
    except ValueError as exc:
        raise HTTPException(422, '日期必须是有效的历史或当日日期，date/date1 不可冲突') from exc
    try:
        return ok(await auction.review(selected, days))
    except Exception as exc:
        raise HTTPException(503, '竞价数据暂不可用') from exc


@router.get('/stock/{code}/detail')
async def stock_detail(code: str, date1: str | None = None, n: int = Query(120, ge=10, le=320)):
    from app.services import auction, stock_detail as service
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        raise HTTPException(422, '股票代码必须是6位数字')
    try:
        day = auction.day_iso(date1) if date1 else None
        if day and day > auction.now_local().date().isoformat():
            raise ValueError('Future date')
    except ValueError as exc:
        raise HTTPException(422, '无效日期') from exc
    return ok(await service.detail(code, day, n))


@router.get('/stock/{code}/intraday')
async def stock_intraday(code: str, date1: str | None = None):
    from app.services import auction, stock_intraday as service
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        raise HTTPException(422, '股票代码必须是6位数字')
    try:
        day = auction.day_iso(date1) if date1 else None
        if day and day > auction.now_local().date().isoformat():
            raise ValueError('Future date')
    except ValueError as exc:
        raise HTTPException(422, '无效日期') from exc
    return ok(await service.review(code, day))


@router.get("/review/uplimit/hot")
async def review_uplimit_hot(date1: str | None = None, limit: int = 100, board: str | None = None):
    """复盘·涨停榜：东财涨停池为主，同花顺补涨停原因/封板统计。

    数据融合策略：
    - 基础字段（代码/名称/价格/连板/封单）来自东财（结构稳定、可回溯）
    - reason（涨停原因）、high_days（N天M板）、open_num（开板次数）、
      suc_rate（封板成功率）来自同花顺（东财 zttj 字段已收缩，无原因文字）
    """
    from app.config import settings
    requested = date1.replace('-', '') if date1 else None
    d = requested or datetime.now().strftime("%Y%m%d")
    fell_back = False
    data = pools.published('up', d, allow_stale=not bool(date1))
    if data is None and settings.collector_mode == 'embedded':
        try:
            data = await pools.require('up', d, allow_stale=not bool(date1))
        except Exception:
            data = None
    if board and _THS_BOARD_RE.match(board):
        # ml 页涨停梯队：原站矩阵形态（plate/plate_stocks/ban_info/max_count/stocks）
        if data is not None and (not requested or data['date'].replace('-', '') == requested):
            day = data['date']
            pool_d = day.replace('-', '')
        elif requested:
            day = f'{requested[:4]}-{requested[4:6]}-{requested[6:]}'
            pool_d = None
        else:
            day = datetime.now().date().isoformat()
            pool_d = None
        snap = store.review_uplimit_get(day)
        if snap is not None:
            # 快照保真路径：原站当日跟踪板块矩阵（含 803xxx），按已解码过滤语义推导
            derived = _derive_board_from_snapshot(snap, board, day)
            rows = (derived.get("plate_stocks") or {}).get(board) or []
            if rows:
                return ok(derived)
            # 快照无本题材行时回退本地池∩成分，避免历史列空壳
        # 无可用快照行：本地计算（池∩板块成分；东财缺则同花顺）
        return ok(_uplimit_hot_board(board, day, pool_d))
    if data is None:
        return ok({"total": 0, "date": d, "items": [], "reason_source": "unavailable", "fell_back": False,
                   "source": "published_limit_pools", "status": "missing"})
    if requested and data['date'].replace('-', '') != requested:
        return ok({"total": 0, "date": requested, "items": [], "reason_source": "unavailable", "fell_back": False,
                   "source": "published_limit_pools", "status": "missing"})
    d = data['date'].replace('-', '')
    pool = data['pool']
    fell_back = bool(requested is None and data['date'].replace('-', '') != datetime.now().strftime('%Y%m%d'))

    ths_snap = pools.published('ths_up', data['date'])
    ths_map = {s['code']: s for s in (ths_snap or {}).get('pool') or []}

    if board:
        pool = [s for s in pool if (s.get("hybk") or "") == board]

    items = []
    for s in pool[:limit]:
        extra = ths_map.get(str(s.get("c", ""))) or {}
        items.append({
            "stock_code": s.get("c", ""),
            "stock_name": "".join((s.get("n") or "").split()),
            "pct": s.get("zdp", 0),
            "price": round(float(s.get("p", 0)) / 1000, 2),
            "amount": s.get("amount", 0),
            "lbc": s.get("lbc", 0),
            "fbt": s.get("fbt", ""),
            "lbt": s.get("lbt", ""),
            "zttj": s.get("zttj", {}),
            "hybk": s.get("hybk", ""),
            "reason": extra.get("reason", ""),
            "high_days": extra.get("high_days", ""),
            "open_num": extra.get("open_num", 0),
            "suc_rate": extra.get("suc_rate", 0),
            "limit_up_type": extra.get("limit_up_type", ""),
        })
    return ok({"total": data.get("total", 0), "date": d, "items": items, "reason_source": "10jqka" if ths_map else "unavailable", "fell_back": fell_back, "source": "published_limit_pools", "status": "ok"})


def _ts2hm(value) -> str:
    """时间戳或 HH:MM 字符串 -> HH:MM（对齐 ths 接口两种返回格式）。"""
    if value in (None, "", 0, "0"):
        return ""
    if isinstance(value, str):
        s = value.strip()
        if ":" in s:  # 已经是 HH:MM / HH:MM:SS
            return s[:5]
        if s.isdigit():
            value = int(s)
        else:
            return ""
    try:
        import time
        return time.strftime("%H:%M", time.localtime(int(value)))
    except (ValueError, TypeError, OverflowError):
        return ""


def _fmt_fbt(value) -> str:
    """EM fbt (HHMMSS int like 92503 -> 09:25) or string time -> HH:MM."""
    if value in (None, "", 0, "0"):
        return ""
    if isinstance(value, str):
        s = value.strip()
        if ":" in s:
            return s[:5]
        if s.isdigit():
            value = int(s)
        else:
            return ""
    try:
        n = int(value)
        return f"{n // 10000:02d}:{n // 100 % 100:02d}"
    except (ValueError, TypeError):
        return ""


@router.get("/review/uplimit/board-hot")
async def review_uplimit_board_hot(date1: str | None = None, plate_type: str = "concept"):
    """复盘·板块涨停热度。默认只读已发布涨停池和板块快照。"""
    from app.services import board_hot
    if plate_type not in ("concept", "industry"):
        raise HTTPException(422, "plate_type 只支持 concept 或 industry")
    try:
        if date1:
            from app.services import auction
            auction.day_iso(date1)
    except ValueError as exc:
        raise HTTPException(422, "无效日期") from exc
    return ok(await board_hot.review(date1, plate_type))


@router.get("/review/uplimit/reason")
async def review_uplimit_reason_open(date1: str | None = None, page: int = Query(1, ge=1, le=50), page_size: int = Query(20, ge=1, le=100)):
    from app.services import board_hot
    try:
        if date1:
            from app.services import auction
            auction.day_iso(date1)
    except ValueError as exc:
        raise HTTPException(422, "无效日期") from exc
    data = await board_hot.reason_groups(date1, page, page_size)
    return ok(data["items"] if data.get("status") != "missing" else [])


@router.get("/referral/config")
async def referral_config():
    return ok({"reward_points": 20})


@router.get("/sentiment/media/ths2/top")
async def sentiment_media_ths2_top(date1: str | None = None, top_n: int = 100):
    """人气榜：只读已发布同花顺热股快照。公开源不能按历史日期回放。"""
    from app.services import popular
    try:
        if date1:
            from app.services import auction
            auction.day_iso(date1)
    except ValueError as exc:
        raise HTTPException(422, "无效日期") from exc
    data = await popular.review(date1, top_n)
    # 原站前端读 data.data；同时保留完整快照元数据。
    return ok({"data": data.get("items") or [], **{k: v for k, v in data.items() if k != "items"}, "items": data.get("items") or []})
