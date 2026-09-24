"""题材数据接口。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
import asyncio
import math

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from app.core import deps
from pydantic import BaseModel

from app.core.errors import ok
from app.services import topic
from app.datasources import eastmoney, tencent
from app.datasources.codes import normalize_code


router = APIRouter(prefix="/v3/topic", tags=["topic"])


def _user_id(user: dict | None) -> str | None:
    return str(user["id"]) if user and user.get("id") else None


@router.get("/home-topics")
async def home_topics(date: str = "", limit: int = 3):
    """首页题材三卡片；date=YYYY-MM-DD 可查历史（读当日快照）。"""
    try:
        return ok(await topic.home_topics(limit=limit, date=date or None))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/home-topics/history-dates")
async def home_topics_history_dates(limit: int = 60):
    """可选历史日期（有当日题材快照的交易日，最近优先）。"""
    from app.core.store import store
    dates = store.topic_snapshot_dates(max(1, min(int(limit or 60), 200)))
    return ok({"dates": dates, "count": len(dates)})


@router.get("/tables")
async def tables(q: str = "", limit: int = 20, page: int = 1, user = Depends(deps.optional_user)):
    """用户题材表格列表（原站口径：默认20条按updated_time倒序，limit<=100，q搜全部匹配）。

    page>1 原站需题材表格 VIP；本地对登录用户开放。
    """
    from app.services import topic_tables
    from app.services import user_service
    limit = max(1, min(int(limit or 20), 100))
    page = max(1, int(page or 1))
    if page > 1 and not user:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "该功能需要订阅题材表格VIP权限"})
    data = topic_tables.list_tables(q=q, user_id=_user_id(user))
    items = data.get("items") or []
    if page > 1 and not user_service.has_subscription(user["id"], "topic_table"):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "该功能需要订阅题材表格VIP权限"})
    start = (page - 1) * limit
    data["items"] = items[start:start + limit]
    data["data"] = data["items"]
    data["total"] = len(items)
    data["page"] = page
    data["limit"] = limit
    return ok(data)


@router.get("/tables/rank")
async def tables_rank(date2: str = "", n_days: int = 1, limit: int = 10, sort_by: str = "rate"):
    """题材热度排行：只读逐日已发布结果，缺日不补零。"""
    from app.services import topic_tables
    from app.core.store import store
    try:
        data = topic_tables.rank_tables(date2=date2, n_days=n_days, limit=limit, sort_by=sort_by, store_obj=store)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return ok(data)


@router.post("/monitor/trigger")
async def monitor_trigger():
    """手动触发监控（原站需登录）：立即执行一次盘中轮询并广播提醒。"""
    from app.services import topic_monitor
    topic.home_topics.cache_clear() if hasattr(topic.home_topics, "cache_clear") else None
    asyncio.create_task(topic.home_topics())
    if topic_monitor._in_session() and topic_monitor.hub.clients:
        topics = topic_monitor._monitored_topics()
        quotes = await topic_monitor._snapshot_quotes()
        if topics and quotes:
            alerts, _ = topic_monitor._alerts_for(topics, quotes, {})
            for a in alerts:
                await topic_monitor.hub.broadcast(a)
    return {"code": 20000, "message": "触发成功", "data": None}


@router.post("/monitor/refresh")
async def monitor_refresh():
    """刷新监控名单（原站需登录）：重置轮询基线，下一轮重新计算全部差值。"""
    from app.services import topic_monitor
    topic_monitor.ensure_loop()
    return {"code": 20000, "message": "刷新成功", "data": None}


@router.post("/monitor/trigger_manual")
async def monitor_trigger_manual(payload: dict = None):
    """全景沙盘个股手动点火买入（原站需登录+订阅）。记录模拟买入并广播刷新。"""
    from app.services import quant as quant_service, topic_monitor
    payload = payload or {}
    code = str(payload.get("code") or "").strip()
    if not code:
        return {"code": 400, "msg": "缺少股票代码", "data": None}
    try:
        rec = quant_service.manual_buys_add(code, payload.get("name") or "", payload.get("price"))
    except Exception as e:
        return {"code": 400, "msg": f"点火失败: {e}", "data": None}
    try:
        await topic_monitor.hub.broadcast({"type": "panorama_tick", "time": datetime.now().strftime("%H:%M:%S"), "manual": rec})
    except Exception:
        pass
    return {"code": 200, "msg": "手动点火成功", "data": rec}


@router.websocket("/ws/topic_monitor")
async def ws_topic_monitor(websocket: WebSocket, token: str = ""):
    """题材监控推送（原站 wss://api.zizizaizai.com/v3/ws/topic_monitor?token=）。

    消息：type=topic_alert {topic, trigger{time,name,code,reason,pct,score,score_str}, leader}
    鉴权失败发送 type=error 后关闭；客户端 5s 重连（原站行为）。
    """
    from app.core import auth
    from app.services import user_service
    await websocket.accept()
    payload = auth.verify_token(token or websocket.query_params.get("token", ""), "access")
    if not payload or not user_service._get_user(payload.get("sub")):
        await websocket.send_json({"type": "error", "message": "请先登录"})
        await websocket.close()
        return
    from app.services import topic_monitor
    await topic_monitor.hub.join(websocket)
    topic_monitor.ensure_loop()
    try:
        while True:
            # 客户端只发 ping/keepalive；收到任何消息即忽略
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        topic_monitor.hub.leave(websocket)


@router.get("/table/{unique_key}/kline")
async def topic_kline(unique_key: str, date1: str = "", n: int = 30, user = Depends(deps.optional_user)):
    """题材K线：仅展示有连续验证收益和点时成员证据的日净值折线。"""
    from app.services import user_service
    if not user or not user_service.has_subscription(user["id"], "topic_kline"):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "题材表格 VIP（K线）需要订阅"})
    n = max(5, min(int(n or 30), 120))
    item = topic.snapshot_item(unique_key, date1)
    from app.services import topic_tables
    table_item = None if item else topic_tables.get_table(unique_key, user_id=_user_id(user))
    if not item and not table_item:
        return ok({"status": "missing", "name": unique_key, "kline": [], "x": [], "y": [], "note": "topic snapshot or user table missing"})
    nav = topic_tables.topic_daily_nav(unique_key, date1=date1, n=n)
    title = (item or table_item).get("name")
    if nav["status"] != "ok":
        return ok({"name": title, "unique_key": unique_key, **nav, "kline": {"x": [], "y": [], "vol": []}})
    return ok({
        "name": title,
        "unique_key": unique_key,
        **nav,
        "kline": {"x": nav["x"], "y": nav["y"], "vol": nav["vol"]},
        "note": "validated ML-R1 daily NAV; OHLC unavailable unless a verified index OHLC series is published",
    })


@router.post("/jygs/parse")
async def jygs_parse(payload: dict = None):
    """韭研公社文章识别：URL/ID -> {title, content, source, article_id, create_time, update_time, images}。"""
    from app.services import jygs
    payload = payload or {}
    url = str(payload.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail={"code": 40000, "message": "请输入韭研公社链接"})
    try:
        data = await jygs.parse(url)
    except ValueError as e:
        return {"code": 40000, "message": str(e), "data": None}
    return ok(data)


@router.post("/table/ocr")
async def table_ocr(image: UploadFile | None = File(None), image_url: str = Form(""), engine: str = Form("qwen")):
    """表格图片 OCR（原站用通义 qwen-vl）。multipart image 或 image_url。"""
    from app.services import vlm
    image_bytes = None
    if image is not None:
        image_bytes = await image.read()
    try:
        rows = await vlm.table_ocr(image_bytes=image_bytes, image_url=image_url or None, engine=engine)
        return ok(rows)
    except ValueError as e:
        return {"code": 40000, "message": str(e), "data": str(e)}


@router.post("/table/merge_content")
async def table_merge_content(payload: dict = None):
    """AI 内容融合：参考文章增量并入当前内容。未配置 AI 时原样返回。"""
    from app.services import vlm
    payload = payload or {}
    article_id = str(payload.get("article_id") or "").strip()
    current_content = str(payload.get("current_content") or "")
    if not article_id:
        return {"code": 40000, "message": "缺少 article_id", "data": None}
    try:
        merged = await vlm.merge_content(article_id, current_content)
        return ok({"merged_content": merged})
    except ValueError as e:
        return {"code": 40000, "message": str(e), "data": None}


@router.post("/tables/top/sort")
async def tables_top_sort(payload: dict = None):
    """置顶题材表拖拽排序保存：{ids: [...]}。"""
    from app.core.store import store
    from app.services import topic_tables
    payload = payload or {}
    ids = payload.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return {"code": 40000, "message": "ids 不能为空", "data": None}
    order = {}
    for rank, key in enumerate(ids):
        item = topic_tables.set_top(str(key), True)
        if item:
            order[item["unique_key"]] = rank
    store.kv_set("topic_pinned_order", order)
    return {"code": 20000, "message": "排序保存成功", "data": None}


@router.post("/events")
async def topic_events(payload: dict = None):
    """前端埋点（原站匿名上报）：仅记录，不影响业务。"""
    from app.core.store import store
    payload = payload or {}
    event_name = str(payload.get("event_name") or "").strip()
    if not event_name:
        return {"code": 40000, "message": "event_name 不能为空", "data": None}
    events = store.kv_get("topic_events", []) or []
    events.append({
        "event_name": event_name,
        "anonymous_id": payload.get("anonymous_id"),
        "payload": payload.get("payload") or payload.get("properties") or {},
        "time": datetime.now().isoformat(timespec="seconds"),
    })
    store.kv_set("topic_events", events[-2000:])
    return {"code": 20000, "message": "ok", "data": None}


class TopicPayload(BaseModel):
    name: str = ""
    content: str = ""
    rows: list = []


class StockReturnsPayload(BaseModel):
    stocks: list[str] = []


@router.post("/table/stock-returns")
async def table_stock_returns(payload: StockReturnsPayload):
    """批量个股涨幅：只读已发布收盘，不现场搜东财或拉日K。"""
    from app.services import topic_tables
    names = list(dict.fromkeys(payload.stocks or []))[:50]
    data = topic_tables.stock_returns(names)
    return ok(data)


@router.post("/table/create")
async def table_create(payload: TopicPayload, user = Depends(deps.optional_user)):
    from app.services import topic_tables
    rec = topic_tables.create_table({"name": payload.name, "content": payload.content, "rows": payload.rows}, user_id=_user_id(user))
    return ok(rec)


@router.get("/table/{unique_key}")
async def table_get(unique_key: str, user = Depends(deps.optional_user)):
    from app.services import topic_tables
    rec = topic_tables.get_table(unique_key, user_id=_user_id(user))
    if not rec:
        return ok({"status": "missing", "id": unique_key})
    return ok(rec)


@router.post("/table/{unique_key}")
async def table_save(unique_key: str, payload: TopicPayload, user = Depends(deps.optional_user)):
    from app.services import topic_tables
    rec = topic_tables.save_table(unique_key, {"name": payload.name, "content": payload.content, "rows": payload.rows}, user_id=_user_id(user))
    if not rec:
        return ok({"status": "missing", "id": unique_key})
    return ok(rec)


@router.post("/table/{unique_key}/top")
async def table_top(unique_key: str, user = Depends(deps.optional_user)):
    from app.services import topic_tables
    rec = topic_tables.set_top(unique_key, True, user_id=_user_id(user))
    return ok(rec or {"status": "missing"})


@router.post("/table/{unique_key}/untop")
async def table_untop(unique_key: str, user = Depends(deps.optional_user)):
    from app.services import topic_tables
    rec = topic_tables.set_top(unique_key, False, user_id=_user_id(user))
    return ok(rec or {"status": "missing"})


@router.post("/table/{unique_key}/delete")
async def table_delete(unique_key: str, user = Depends(deps.optional_user)):
    from app.services import topic_tables
    rec = topic_tables.set_deleted(unique_key, True, user_id=_user_id(user))
    return ok(rec or {"status": "missing"})


@router.post("/table/{unique_key}/restore")
async def table_restore(unique_key: str, user = Depends(deps.optional_user)):
    from app.services import topic_tables
    rec = topic_tables.set_deleted(unique_key, False, user_id=_user_id(user))
    return ok(rec or {"status": "missing"})


@router.get("/follows")
async def topic_follows(user = Depends(deps.optional_user)):
    from app.services import topic_tables
    return ok(topic_tables.follows_digest(user_id=_user_id(user)))


@router.get("/follow-alerts")
async def topic_follow_alerts(limit: int = 50, user = Depends(deps.optional_user)):
    """关注题材最近变化提醒。"""
    from app.services import topic_tables
    return ok(topic_tables.follow_alerts(max(1, min(int(limit or 50), 200)), user_id=_user_id(user)))


@router.post("/follow-alerts/read")
async def topic_follow_alerts_read(payload: dict = None, user = Depends(deps.current_user)):
    """标记提醒已读（alert_ids 数组，id 形如 unique_key:date）。"""
    from app.services import topic_tables
    payload = payload or {}
    ids = payload.get("alert_ids") or []
    n = topic_tables.mark_alerts_read(ids, user_id=_user_id(user))
    return ok({"read": n, "status": "ok"})


@router.get("/table/{unique_key}/continuity")
async def topic_table_continuity(unique_key: str, days: int = 5):
    """题材持续性卡片（VIP 展示，本地对关注用户开放）。"""
    from app.services import topic_follow
    return ok(topic_follow._continuity(unique_key, max(2, min(int(days or 5), 30))))


@router.get("/follows/status")
async def topic_follows_status(topic_ids: str = "", user = Depends(deps.optional_user)):
    from app.services import topic_tables
    ids = [x.strip() for x in topic_ids.split(",") if x.strip()]
    return ok(topic_tables.follow_status(ids, user_id=_user_id(user)))


@router.post("/follows/{unique_key}")
async def topic_follow(unique_key: str, user = Depends(deps.current_user)):
    from app.services import topic_tables
    return ok(topic_tables.toggle_follow(unique_key, True, user_id=_user_id(user)))


@router.delete("/follows/{unique_key}")
async def topic_unfollow(unique_key: str, user = Depends(deps.current_user)):
    from app.services import topic_tables
    return ok(topic_tables.toggle_follow(unique_key, False, user_id=_user_id(user)))


@router.get("/table/{unique_key}/stocks/popular")
async def topic_stocks_popular(unique_key: str, date1: str = "", limit: int = 100, user = Depends(deps.optional_user)):
    """题材成分人气：只读已发布题材快照中的涨停成分，并用已发布热股榜补排名。"""
    from app.services import popular as popular_svc
    item = topic.snapshot_item(unique_key, date1)
    from app.services import topic_tables
    table_item = None if item else topic_tables.get_table(unique_key, user_id=_user_id(user))
    if not item and not table_item:
        return ok({"data": [], "items": [], "status": "missing", "date": date1, "source": "published_topic_snapshots"})
    if not item and date1 and date1.replace('-', '') != datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d'):
        return ok({"data": [], "items": [], "status": "missing_historical_members", "date": date1,
                   "source": "user_topic_tables", "note": "自建题材缺该日成员版本，不能用当前成员回填历史人气"})
    stocks = (item.get("stocks") if item else topic_tables.member_codes(table_item)) or []
    day = date1 or ((item or {}).get('trade_date') if item else ((table_item or {}).get('quote') or {}).get('date'))
    hot = await popular_svc.review(day, 100)
    hot_map = {str(r.get('symbol_code')): r for r in (hot.get('items') or [])}
    rows = []
    for s in stocks:
        code = str(s.get('code') or '')
        hit = hot_map.get(code) or {}
        rows.append({
            "stock_code": code,
            "stock_name": s.get("name") or hit.get('symbol_name') or code,
            "rank": hit.get('rank') if hit else None,
            "rank_diff": hit.get('rank_diff'),
            "px_change_rate": s.get("pct") if s.get('pct') is not None else hit.get('last_pct'),
            "lbc": s.get("lbc"),
            "reason": s.get("reason"),
        })
    rows.sort(key=lambda r: (r['rank'] is None, r['rank'] if r['rank'] is not None else 9999))
    rows = rows[: max(1, min(int(limit or 100), 200))]
    return ok({"data": rows, "items": rows, "status": "ok", "date": date1 or (item.get('trade_date') if item else (table_item.get('quote') or {}).get('date')), "source": "published_topic_snapshots+popular" if item else "user_topic_tables+popular"})


@router.get("/table/{unique_key}/stocks/pct/batch")
async def topic_stocks_pct_batch(unique_key: str, dates: str = "", days: int = 10, user = Depends(deps.optional_user)):
    """题材成分股区间涨幅：只读已发布收盘快照。（VIP：涨幅区间需订阅 pct_interval_vip）"""
    from app.services import user_service
    if not user or not user_service.has_subscription(user["id"], "pct_interval_vip"):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "涨幅区间 VIP 需要订阅"})
    from app.services import ml_r1_service, topic_tables
    try:
        day_list = [topic_tables._iso(d.strip()) for d in dates.split(',') if d.strip()]
        if len(day_list) > 15 or days not in (5, 10, 20):
            raise ValueError('最多15个日期，周期仅支持5/10/20日')
        if not day_list:
            raise ValueError('必须指定日期')
        result = ml_r1_service.pct(15, unique_key, day_list, days)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    out = {d: value.get('items', []) for d, value in result.items()}
    return ok({'status': 'ok' if any(out.values()) else 'missing_input', 'items': out, 'data': out,
               'meta': {d: value['meta'] for d, value in result.items()},
               'source': 'ml_r1_prepared', 'formula_version': ml_r1_service.VERSION})


@router.post("/table/{unique_key}/kline/sync")
async def topic_kline_sync(unique_key: str, user = Depends(deps.current_user)):
    """题材K线同步请求：只触发已授权的预计算读取。"""
    from app.services import user_service
    if not user_service.has_subscription(user["id"], "topic_kline"):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "题材表格 VIP（K线）需要订阅"})
    asyncio.create_task(topic_kline(unique_key, user=user))
    return {"code": 20000, "message": "同步任务已启动"}
