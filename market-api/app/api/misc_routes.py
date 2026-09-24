"""情绪/用户/订阅/支付/管理员/机器人等接口。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, Depends, File, Header, HTTPException, UploadFile
from pydantic import BaseModel
from app.core import deps

from app.core.errors import ok, fail, PARAM_ERROR
from app.datasources import eastmoney, tencent
from app.services import intraday, sentiment, subscription, timing


router = APIRouter(tags=["misc"])


def _user_has_sub(user, module_code):
    if not user:
        return False
    from app.services import user_service
    return user_service.has_subscription(user["id"], module_code)


# ---- 情绪周期（VIP 能力，数据同公开情绪源）----
@router.get("/v3/api/sentiment/kline/day/{model}")
async def sentiment_kline(model: int, date1: str | None = None, user: dict | None = Depends(deps.optional_user)):
    """首页周期图读 x/index；VIP 读 items 里的本地情绪K代理。model=1 为 VIP 版本，需订阅。"""
    if model == 1 and not _user_has_sub(user, "sentiment_vip"):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "情绪周期 VIP 需要订阅"})
    return ok(await sentiment.sentiment_kline_day(model, date1))


@router.get("/v3/sentiment/data")
async def sentiment_data(date1: str | None = None):
    """当日 20 字段情绪面板（对齐原站结构：fb_num/bigleg_num/lb_2_num/...）。"""
    return ok(await intraday.sentiment_data(date1))


@router.get("/v3/api/sentiment/trend/{kind}")
async def sentiment_trend(kind: int, date1: str | None = None):
    """分钟级情绪趋势：0=主要情绪 1=敏感情绪。[[HH:MM:SS, value, amount], ...]"""
    return ok(await intraday.trend(kind, date1))


@router.get("/v3/sentiment/distribute/trend")
async def sentiment_distribute_trend(date1: str | None = None):
    """分钟级涨跌分布（原站情绪页数据源）。"""
    return ok(await intraday.distribute_trend(date1))


@router.get("/v3/sentiment/market/data")
async def sentiment_market_data(date1: str | None = None, with_zs: str | None = None):
    """炸板率图：只读已发布涨停/炸板池和指数快照，不再现场扫描全市场日K。"""
    from app.core.store import store
    from app.services.market import trade_days as _trade_days
    from app.services import pools, index_feed
    n_days = 120
    days = await _trade_days(n_days + 5)
    if date1:
        cutoff = date1.replace("-", "")
        days = [d for d in days if d >= cutoff]
    days = days[-n_days:]
    rows = []
    for d in days:
        iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        up = pools.published('up', iso)
        broken = pools.published('broken', iso)
        agg = store.kv_get('sentiment_agg:' + d) or {}
        row = {"date1": iso}
        if up is not None:
            pool = [s for s in up.get('pool') or [] if str(s.get('c','')).startswith(('0','3','6'))]
            row["uplimit_num"] = len(pool)
            row["uplimit_n_num"] = sum(1 for s in pool if int(s.get('lbc') or 1) == 1)
        elif isinstance(agg, dict) and agg:
            row["uplimit_num"] = agg.get('zt')
            row["uplimit_n_num"] = agg.get('first')
        if broken is not None:
            row["zb_num"] = len([s for s in broken.get('pool') or [] if str(s.get('c') or s.get('code') or '').startswith(('0','3','6'))])
        else:
            zb = store.kv_get('zb_num:' + d)
            if isinstance(zb, int):
                row["zb_num"] = zb
        rows.append(row)
    zs = []
    if with_zs:
        latest = index_feed.published()
        # Historical index closes are not in the latest intraday snapshot; leave unknown unless kv exists.
        zs = [None for _ in days]
    result = {
        "categoryData": [r.get("date1") for r in rows],
        "uplimit_n_num": [r.get("uplimit_n_num") for r in rows],
        "uplimit_num": [r.get("uplimit_num") for r in rows],
        "zb_num": [r.get("zb_num") for r in rows],
        "zb_pct": [
            round(r["zb_num"] / (r["zb_num"] + r["uplimit_num"]) * 100, 2)
            if r.get("zb_num") is not None and r.get("uplimit_num") not in (None, 0) else None
            for r in rows
        ],
        "zs": zs,
        "source": "published_limit_pools",
        "status": "partial" if any(r.get("uplimit_num") is None for r in rows) else "ok",
    }
    return ok(result)


@router.get("/v3/sentiment/timing")
async def sentiment_timing(date1: str | None = None):
    """VIP 逐日择时。返回列表以兼容原站 forEach(date1/market_timing)。"""
    data = await intraday.sentiment_timing(date1)
    return ok(data["items"])


@router.get("/v3/sentiment/distribute_timing")
async def sentiment_distribute_timing(date1: str | None = None):
    """分钟择时分布：只读已发布 minute_samples，缺日 missing。"""
    data = await intraday.distribute_timing(date1)
    return ok(data["items"] if data.get("status") != "missing" else data)


@router.get("/v3/api/timing/market/style")
async def timing_market_style(date1: str | None = None):
    """风格择时（原站 chunk-52b22ba5 / 情绪-周期-年 页）。

    逐日 {date, score, score2, timing}; score 为自行设计的市场得分模型
    （原站接口已下线, 无公开算法）: 涨停家数 40 + 连板高度 25 + 赚钱效应 20 + 情绪强度 15。
    """
    return ok(await timing.market_style(date1))


@router.get("/v3/api/timing/market/style2")
async def timing_market_style2(date1: str | None = None):
    """市场结构周期2: 成交额/涨跌家数序列。"""
    return ok(await timing.market_style2(date1))

# ---- 认证 / 支付 / 订阅（用户体系，Bearer 鉴权）----


def _current_user(authorization: str = Header(default="")) -> dict:
    """Bearer 鉴权依赖；失败抛 401，前端跳登录页。"""
    from fastapi import HTTPException
    from app.core import auth as auth_util
    from app.services import user_service
    token = authorization[7:] if authorization.startswith("Bearer ") else authorization
    payload = auth_util.verify_token(token, "access")
    if not payload:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "未登录或登录已过期"})
    row = user_service._get_user(payload["sub"])
    if not row:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "用户不存在"})
    return user_service._user_view(row)


@router.post("/v2/login/register")
async def register(body: dict = Body(default={}), no_redirect: bool = True):
    from app.services import user_service
    try:
        data = user_service.register(
            str(body.get("email", "")), str(body.get("password", "")),
            str(body.get("user_nick", "")), str(body.get("invite_code", "")),
        )
    except ValueError as e:
        return fail(PARAM_ERROR, message=str(e))
    return ok(data)


def _send_email_code(email: str, kind: str) -> dict:
    """生成 6 位验证码，60s 冷却。本地模式把码直接放 dev_code 便于联调；生产接 SMTP。"""
    from app.core.store import store
    import random
    import time as _time
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("请输入有效的邮箱地址")
    key = f"email_code_{kind}_{email}"
    rec = store.kv_get(key, {}) or {}
    now = _time.time()
    if rec.get("sent_at") and now - float(rec["sent_at"]) < 60:
        raise ValueError("发送过于频繁，请稍后再试")
    code = f"{random.randint(0, 999999):06d}"
    store.kv_set(key, {"code": code, "sent_at": now})
    return {"email": email, "dev_code": code, "expires_in": 600}


@router.post("/v2/login/email/send_code")
async def email_send_code(body: dict = Body(default={})):
    try:
        data = _send_email_code(str(body.get("email") or ""), "register")
    except ValueError as e:
        return fail(PARAM_ERROR, message=str(e))
    return ok(data)


@router.post("/v2/login/email/reset/send_code")
async def email_reset_send_code(body: dict = Body(default={})):
    try:
        data = _send_email_code(str(body.get("email") or ""), "reset")
    except ValueError as e:
        return fail(PARAM_ERROR, message=str(e))
    return ok(data)


@router.post("/v2/login/login")
async def login(body: dict = Body(default={}), no_redirect: bool = True):
    from app.services import user_service
    try:
        data = user_service.login(str(body.get("email", "")), str(body.get("password", "")))
    except ValueError as e:
        return fail(PARAM_ERROR, message=str(e))
    return ok(data)


@router.post("/v2/login/logout")
async def logout(no_redirect: bool = True):
    return ok({"status": "logged_out"})


@router.post("/v2/login/refresh")
async def refresh(body: dict = Body(default={}), no_redirect: bool = True):
    from app.services import user_service
    try:
        data = user_service.refresh(str(body.get("refresh_token", "")))
    except ValueError as e:
        return fail(PARAM_ERROR, message=str(e))
    return ok(data)


@router.get("/user/profile/me")
async def profile_me(user: dict = Depends(_current_user), no_redirect: bool = True):
    from app.services import user_service
    return ok(user_service.profile(user["id"]))

class PasswordChangeBody(BaseModel):
    old_password: str = ""
    new_password: str = ""


@router.post("/v3/user/profile/password")
async def change_password(body: PasswordChangeBody, user: dict = Depends(_current_user)):
    from app.services import user_service
    try:
        data = user_service.change_password(user["id"], body.old_password, body.new_password)
    except ValueError as e:
        return fail(PARAM_ERROR, message=str(e))
    return ok(data)


ALLOWED_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _is_valid_image(data: bytes, content_type: str) -> bool:
    if content_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if content_type == "image/webp":
        return len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


@router.post("/v3/user/profile/avatar/upload")
async def avatar_upload(file: UploadFile = File(...), user: dict = Depends(_current_user)):
    """头像上传（原站契约：multipart file 字段，返回 {url}，同时更新 users.avatar_url）。"""
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        return fail(PARAM_ERROR, message="仅支持 jpg/png/gif/webp 图片")
    data = await file.read()
    if not data:
        return fail(PARAM_ERROR, message="文件为空")
    if len(data) > MAX_IMAGE_BYTES:
        return fail(PARAM_ERROR, message="图片不能超过 5MB")
    if not _is_valid_image(data, file.content_type):
        return fail(PARAM_ERROR, message="文件内容与图片格式不符")
    import secrets as _secrets
    from pathlib import Path as _Path
    from app.services import user_service
    ext = ALLOWED_IMAGE_TYPES[file.content_type]
    name = "avatar_" + user["id"][:8] + "_" + _secrets.token_hex(6) + ext
    uploads_dir = _Path(__file__).resolve().parents[2] / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    (uploads_dir / name).write_bytes(data)
    url = "/uploads/" + name
    try:
        data_out = user_service.set_avatar(user["id"], url)
    except ValueError as e:
        return fail(PARAM_ERROR, message=str(e))
    return ok(data_out)


@router.post("/user/profile/email/code")
async def profile_email_code(body: dict = Body(default={}), user: dict = Depends(_current_user)):
    """换绑邮箱第 1 步：给新邮箱发验证码（原站契约 POST /user/profile/email/code）。"""
    try:
        data = _send_email_code(str(body.get("email") or ""), "bind")
    except ValueError as e:
        return fail(PARAM_ERROR, message=str(e))
    return ok("验证码已发送")


@router.post("/user/profile/email/bind")
async def profile_email_bind(body: dict = Body(default={}), user: dict = Depends(_current_user)):
    """换绑邮箱第 2 步：校验验证码并更新 users.email（原站契约 POST /user/profile/email/bind）。"""
    from app.core.store import store
    import time as _time
    from app.services import user_service
    email = (str(body.get("email") or "")).strip().lower()
    code = str(body.get("code") or "").strip()
    if not email or "@" not in email:
        return fail(PARAM_ERROR, message="请输入有效的邮箱地址")
    if not code:
        return fail(PARAM_ERROR, message="请输入验证码")
    rec = store.kv_get(f"email_code_bind_{email}", {}) or {}
    if not rec.get("code") or rec["code"] != code:
        return fail(PARAM_ERROR, message="验证码错误")
    if _time.time() - float(rec.get("sent_at") or 0) > 600:
        return fail(PARAM_ERROR, message="验证码已过期，请重新获取")
    try:
        data = user_service.update_email(user["id"], email)
    except ValueError as e:
        return fail(PARAM_ERROR, message=str(e))
    # 绑定成功后消费验证码
    store.kv_set(f"email_code_bind_{email}", {"code": "", "sent_at": 0})
    return ok(data)


@router.get("/v3/payment/modules")
async def payment_modules():
    return ok(subscription.modules())


@router.get("/v3/payment/settings")
async def payment_settings():
    return ok({"enabled": True, "currency": "CNY", "provider": "simulated"})


@router.post("/v3/payment/order/create")
async def order_create(body: dict = Body(default={}), user: dict = Depends(_current_user)):
    from app.services import user_service
    try:
        data = user_service.create_order(
            user["id"], str(body.get("module_code", "")),
            int(body.get("months", 1) or 1), str(body.get("coupon_code", "")),
            bool(body.get("use_points", False)))
    except ValueError as e:
        return fail(PARAM_ERROR, message=str(e))
    return ok(data)


@router.post("/v3/payment/order/{order_id}/pay")
async def order_pay(order_id: str, user: dict = Depends(_current_user)):
    """模拟支付回调。正式部署替换为微信支付 notify 验签后调 user_service.pay_order。"""
    from app.services import user_service
    try:
        data = user_service.pay_order(user["id"], order_id)
    except ValueError as e:
        return fail(PARAM_ERROR, message=str(e))
    return ok(data)


@router.get("/v3/payment/orders")
async def my_orders(user: dict = Depends(_current_user)):
    from app.services import user_service
    return ok(user_service.list_orders(user["id"]))


@router.get("/v3/payment/coupon/validate")
async def coupon_validate(code: str = "", user: dict = Depends(_current_user)):
    from app.services import user_service
    return ok(user_service.validate_coupon(user["id"], code))


@router.get("/v3/user/subscriptions")
async def user_subscriptions(user: dict = Depends(_current_user)):
    from app.services import user_service
    return ok(user_service.list_subscriptions(user["id"]))


@router.get("/v3/user/points")
async def user_points(user: dict = Depends(_current_user)):
    return ok({"points": user["points"]})


@router.get("/v3/user/token")
async def user_api_token(user: dict = Depends(_current_user)):
    """API 令牌：无则惰性生成。"""
    from app.services import user_service
    return ok(user_service.api_token_get(user["id"]))


@router.post("/v3/user/token/refresh")
async def user_api_token_refresh(user: dict = Depends(_current_user)):
    """刷新 API 令牌，旧令牌立即失效。"""
    from app.services import user_service
    return ok(user_service.api_token_refresh(user["id"]))


@router.get("/v3/user/referral/stats")
async def referral_stats(user: dict = Depends(_current_user)):
    from app.services import user_service
    return ok(user_service.referral_stats(user["id"]))


@router.get("/v3/user/referral/admin/list")
async def referral_admin_list(page: int = 1, page_size: int = 20, user: dict = Depends(deps.admin_user)):
    from app.services import user_service
    return ok(user_service.referral_admin_list(page, page_size))


@router.post("/v3/user/referral/admin/audit")
async def referral_admin_audit(body: dict = Body(default={}), user: dict = Depends(deps.admin_user)):
    from app.services import user_service
    try:
        return ok(user_service.referral_audit(str(body.get("record_id") or ""), bool(body.get("is_pass"))))
    except ValueError as e:
        return fail(PARAM_ERROR, message=str(e))


@router.post("/v3/user/referral/admin/set_suspicious")
async def referral_admin_set_suspicious(body: dict = Body(default={}), user: dict = Depends(deps.admin_user)):
    from app.services import user_service
    try:
        return ok(user_service.referral_set_suspicious(str(body.get("user_id") or ""), body.get("is_suspicious")))
    except ValueError as e:
        return fail(PARAM_ERROR, message=str(e))


@router.get("/v3/ai-report/list")
async def ai_report_list(type: str = "morning", page: int = 1, page_size: int = 20):
    from app.services import ai_report
    return ok(await ai_report.list_reports(type, page, page_size))


@router.get("/v3/ai-report/detail/{report_id}")
async def ai_report_detail(report_id: str, user: dict | None = Depends(deps.optional_user)):
    from app.services import ai_report
    data = await ai_report.detail(report_id)
    if (data or {}).get("report_type") == "morning" and not _user_has_sub(user, "ai_premarket"):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "AI 每日盘前需要订阅"})
    return ok(data)


@router.post("/v3/ai-report/like")
async def ai_report_like(post_id: str, user: dict | None = Depends(deps.optional_user)):
    """点赞切换：返回 {liked, likes}。"""
    from app.core.store import store
    item = store.ai_report_get(post_id)
    if not item:
        return fail(40400, message="报告不存在")
    uid = (user or {}).get("id") or "guest"
    liked_set = set(store.kv_get("ai_report_likes_" + post_id, []) or [])
    if uid in liked_set:
        liked_set.discard(uid)
        liked = False
    else:
        liked_set.add(uid)
        liked = True
    store.kv_set("ai_report_likes_" + post_id, sorted(liked_set))
    item["likes"] = max(0, int(item.get("likes") or 0)) + (1 if liked else -1)
    item["liked_by_me"] = liked
    store.ai_report_save(item.get("report_type") or item.get("type") or "morning", item.get("trade_date") or "", item)
    return ok({"liked": liked, "likes": item["likes"]})


@router.post("/v3/ai-report/share-sign")
async def ai_report_share_sign(post_id: str, user: dict | None = Depends(deps.optional_user)):
    """生成报告分享邀请码（原站用于分享解锁）。"""
    from app.core.store import store
    import hashlib
    item = store.ai_report_get(post_id)
    if not item:
        return fail(40400, message="报告不存在")
    uid = str((user or {}).get("id") or "guest")
    code = hashlib.sha256(("report:" + post_id + ":" + uid).encode()).hexdigest()[:8].upper()
    signs = store.kv_get("ai_report_share_signs", {}) or {}
    signs[post_id] = {"invite_code": code, "user_id": uid}
    store.kv_set("ai_report_share_signs", signs)
    return ok({"invite_code": code})


@router.get("/v3/topic/monitor/panorama/config")
async def panorama_config(date: str | None = None):
    from app.services.quant import backtest
    data = backtest(window=8)
    crowding = {"available": True, "level": "normal"}
    if (data.get("win_rate_pct") or 0) < 35:
        crowding["level"] = "high"
    return ok({
        "date": date,
        "supplement_h67": False,
        "filter_topic_net5": True,
        "v2_position_fractions": [0.25, 0.25],
        "v3_position_fractions": [0.15, 0.15, 0.08, 0.07, 0.05],
        "crowding": crowding,
        "source": "published_strategy_backtest",
        "note": "position fractions follow the original defaults; crowding uses local limit-up next-day hit rate, not the original live monitor",
    })


@router.get("/v3/topic/monitor/panorama")
async def panorama(date: str | None = None):
    from app.services.quant import strategy_daily_details
    from app.core.store import store
    details = await strategy_daily_details(date)
    day = details.get("date") or date
    topics = store.topic_snapshot_all(day)[:8] if day else []
    return ok({
        "date": day,
        "status": details.get("status"),
        "buys": details.get("records") or [],
        "positions": details.get("positions") or [],
        "topics": [{"name": t.get("name"), "pct": t.get("today_pct"), "limit_up_count": t.get("limit_up_count")} for t in topics],
        "source": "published_strategy_backtest+topic_snapshots",
    })


# ---- 机器人（原站为独立域 stock.ziruxing.com，这里合并到主 API）----
@router.get("/robot/hot")
async def robot_hot():
    return ok(await sentiment.sentiment_today())


@router.get("/robot/base")
async def robot_base():
    return ok(await sentiment.sentiment_series(days=20))


@router.get("/robot/vip")
async def robot_vip():
    return ok({"message": "VIP 机器人（需订阅）"})


@router.get("/v3/api/review/uplimit/reason")
async def review_uplimit_reason(date1: str | None = None, page: int = 1, page_size: int = 20):
    """原站涨停原因页。按板块分组，长文来自已发布同花顺 block_top。"""
    from app.services import board_hot
    data = await board_hot.reason_groups(date1, page, page_size)
    if data.get("status") == "missing":
        return ok([])
    return ok(data["items"])


@router.get("/v3/api/sentiment/market/hot/day")
async def sentiment_market_hot_day(date: str | None = None, date1: str | None = None, modal: int = 0):
    """情绪热度页按天序列。只读已发布涨停池，缺日不回退。"""
    return ok((await sentiment.market_hot_day(date or date1)).get("items") or [])
