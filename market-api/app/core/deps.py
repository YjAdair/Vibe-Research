"""共享鉴权依赖：Bearer 登录 + 订阅模块校验。"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException

from app.core import auth


def current_user(authorization: str = Header(default="")) -> dict:
    """Bearer 鉴权；失败 401。返回 user 视图 dict。"""
    from app.services import user_service
    token = authorization[7:] if authorization.startswith("Bearer ") else authorization
    payload = auth.verify_token(token, "access")
    if not payload:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "未登录或登录已过期"})
    row = user_service._get_user(payload["sub"])
    if not row:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "用户不存在"})
    return user_service._user_view(row)


def optional_user(authorization: str = Header(default="")) -> dict | None:
    """可选登录：无 token 或 token 失效时返回 None，不抛错。"""
    from app.services import user_service
    token = authorization[7:] if authorization.startswith("Bearer ") else authorization
    if not token:
        return None
    payload = auth.verify_token(token, "access")
    if not payload:
        return None
    row = user_service._get_user(payload["sub"])
    return user_service._user_view(row) if row else None


def admin_user(user: dict = Depends(current_user)) -> dict:
    """管理员守卫：非 admin 角色 403。"""
    if (user or {}).get("role") != "admin":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仅管理员可访问"})
    return user


def require_subscription(module_code: str):
    """订阅墙依赖工厂：未订阅返回 403（对应原站付费模块提示）。"""
    def _dep(user: dict = None) -> dict:
        from app.services import user_service
        # FastAPI 注入：user 由 current_user 提供
        if user is None or not user_service.has_subscription(user["id"], module_code):
            raise HTTPException(
                status_code=403,
                detail={"code": 403, "message": f"该功能需要订阅（{module_code}）"},
            )
        return user
    return _dep
