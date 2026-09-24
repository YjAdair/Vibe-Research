"""Admin console routes: user/role/subscription/order management."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends

from app.core.deps import admin_user
from app.core.errors import ok, fail, PARAM_ERROR

router = APIRouter(prefix="/v3/admin", tags=["admin"])


@router.get("/users")
async def admin_users(page: int = 1, page_size: int = 20, user=Depends(admin_user)):
    from app.services import user_service
    try:
        return ok(user_service.admin_list_users(page, page_size))
    except ValueError as e:
        return fail(PARAM_ERROR, message=str(e))


@router.post("/users/{uid}/role")
async def admin_set_role(uid: str, body: dict = Body(default={}), user=Depends(admin_user)):
    from app.services import user_service
    role = str(body.get("role", ""))
    try:
        return ok(user_service.admin_set_role(uid, role))
    except ValueError as e:
        return fail(PARAM_ERROR, message=str(e))


@router.post("/subscription/grant")
async def admin_grant(body: dict = Body(default={}), user=Depends(admin_user)):
    """人工授予订阅（对齐原站人工审核开通场景）。"""
    from app.services import user_service
    try:
        return ok(user_service.admin_grant_subscription(
            str(body.get("user_id", "")), str(body.get("module_code", "")), int(body.get("months", 1) or 1)))
    except ValueError as e:
        return fail(PARAM_ERROR, message=str(e))


@router.post("/subscription/revoke")
async def admin_revoke(body: dict = Body(default={}), user=Depends(admin_user)):
    from app.services import user_service
    try:
        return ok(user_service.admin_revoke_subscription(
            str(body.get("user_id", "")), str(body.get("module_code", ""))))
    except ValueError as e:
        return fail(PARAM_ERROR, message=str(e))


@router.post("/points/adjust")
async def admin_points_adjust(body: dict = Body(default={}), user: dict = Depends(admin_user)):
    """管理员积分调整：{user_id, amount, action(add/reward/deduct), remark}。"""
    from app.services import user_service
    target = str(body.get("user_id") or "")
    try:
        amount = int(body.get("amount") or 0)
    except (TypeError, ValueError):
        return fail(PARAM_ERROR, "amount must be integer")
    action = str(body.get("action") or "add")
    remark = str(body.get("remark") or "")
    if not target:
        return fail(PARAM_ERROR, "user_id required")
    try:
        return ok(user_service.points_adjust(target, amount, action, remark))
    except ValueError as exc:
        return fail(PARAM_ERROR, str(exc))


@router.get("/orders")
async def admin_orders(status: str = "", page: int = 1, page_size: int = 20, user=Depends(admin_user)):
    from app.services import user_service
    try:
        return ok(user_service.admin_list_orders(status, page, page_size))
    except ValueError as e:
        return fail(PARAM_ERROR, message=str(e))


@router.post("/coupon/create")
async def admin_coupon_create(body: dict = Body(default={}), user=Depends(admin_user)):
    from app.services import user_service
    try:
        return ok(user_service.admin_create_coupon(
            int(body.get("discount_percent", 100) or 100),
            int(body.get("max_uses", 1) or 1),
            int(body.get("days_valid", 30) or 30),
            str(body.get("code", ""))))
    except ValueError as e:
        return fail(PARAM_ERROR, message=str(e))


@router.get("/coupon/list")
async def admin_coupon_list(user=Depends(admin_user)):
    from app.services import user_service
    return ok(user_service.admin_list_coupons())


def _rules_kv(kind: str) -> list[dict]:
    from app.core.store import store
    return store.kv_get("admin_rules_" + kind, []) or []


def _rules_save(kind: str, rules: list[dict]) -> None:
    from app.core.store import store
    store.kv_set("admin_rules_" + kind, rules)


@router.get("/rules/points_rate")
async def admin_points_rate_list(user=Depends(admin_user)):
    return ok({"rules": _rules_kv("points_rate")})


@router.post("/rules/points_rate")
async def admin_points_rate_create(body: dict = Body(default={}), user=Depends(admin_user)):
    endpoint = str(body.get("endpoint") or "").strip()
    if not endpoint:
        return fail(PARAM_ERROR, message="endpoint 不能为空")
    rules = _rules_kv("points_rate")
    if any(r.get("endpoint") == endpoint for r in rules):
        return fail(PARAM_ERROR, message="规则已存在")
    rule = {
        "id": max([r.get("id", 0) for r in rules], default=0) + 1,
        "endpoint": endpoint,
        "min_points": int(body.get("min_points") or 0),
        "limit_per_min": int(body.get("limit_per_min") or 60),
        "description": str(body.get("description") or ""),
        "chinese_name": str(body.get("chinese_name") or ""),
    }
    rules.append(rule)
    _rules_save("points_rate", rules)
    return ok(rule)


@router.put("/rules/points_rate/{rule_id}")
async def admin_points_rate_update(rule_id: int, body: dict = Body(default={}), user=Depends(admin_user)):
    rules = _rules_kv("points_rate")
    for r in rules:
        if r.get("id") == rule_id:
            for k in ("endpoint", "chinese_name", "description"):
                if body.get(k) is not None:
                    r[k] = str(body.get(k))
            for k in ("min_points", "limit_per_min"):
                if body.get(k) is not None:
                    r[k] = int(body.get(k))
            _rules_save("points_rate", rules)
            return ok(r)
    return fail(40400, message="规则不存在")


@router.delete("/rules/points_rate/{rule_id}")
async def admin_points_rate_delete(rule_id: int, user=Depends(admin_user)):
    rules = _rules_kv("points_rate")
    left = [r for r in rules if r.get("id") != rule_id]
    if len(left) == len(rules):
        return fail(40400, message="规则不存在")
    _rules_save("points_rate", left)
    return ok({"deleted": rule_id})


@router.get("/rules/subscription_plan")
async def admin_plan_list(user=Depends(admin_user)):
    return ok({"plans": _rules_kv("subscription_plan")})


@router.post("/rules/subscription_plan")
async def admin_plan_create(body: dict = Body(default={}), user=Depends(admin_user)):
    name = str(body.get("name") or "").strip()
    if not name:
        return fail(PARAM_ERROR, message="name 不能为空")
    plans = _rules_kv("subscription_plan")
    plan = {
        "id": max([p.get("id", 0) for p in plans], default=0) + 1,
        "name": name,
        "endpoint": str(body.get("endpoint") or ""),
        "plan_type": str(body.get("plan_type") or ""),
        "limit_per_min": int(body.get("limit_per_min") or 60),
        "price": float(body.get("price") or 0),
        "description": str(body.get("description") or ""),
        "status": int(body.get("status") or 1),
    }
    plans.append(plan)
    _rules_save("subscription_plan", plans)
    return ok(plan)


@router.put("/rules/subscription_plan/{plan_id}")
async def admin_plan_update(plan_id: int, body: dict = Body(default={}), user=Depends(admin_user)):
    plans = _rules_kv("subscription_plan")
    for p in plans:
        if p.get("id") == plan_id:
            for k in ("name", "endpoint", "plan_type", "description"):
                if body.get(k) is not None:
                    p[k] = str(body.get(k))
            for k in ("limit_per_min", "status"):
                if body.get(k) is not None:
                    p[k] = int(body.get(k))
            if body.get("price") is not None:
                p["price"] = float(body.get("price"))
            _rules_save("subscription_plan", plans)
            return ok(p)
    return fail(40400, message="套餐不存在")


@router.delete("/rules/subscription_plan/{plan_id}")
async def admin_plan_delete(plan_id: int, user=Depends(admin_user)):
    plans = _rules_kv("subscription_plan")
    left = [p for p in plans if p.get("id") != plan_id]
    if len(left) == len(plans):
        return fail(40400, message="套餐不存在")
    _rules_save("subscription_plan", left)
    return ok({"deleted": plan_id})
