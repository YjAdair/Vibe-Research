"""用户/认证/订阅/订单服务（商业化 P0 层）。

表：users、orders。subscriptions 复用现有表。
支付当前为模拟闭环（下单→支付→开通）；接微信支付时只需替换
pay_order 里的回调逻辑。
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from app.core import auth
from app.core.store import store
from app.services import subscription


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_tables() -> None:
    with store._conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                user_nick TEXT DEFAULT '',
                avatar_url TEXT DEFAULT '',
                role TEXT DEFAULT 'user',
                points INTEGER DEFAULT 0,
                invite_code TEXT DEFAULT '',
                created_time TEXT,
                updated_time TEXT
            );
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                module_code TEXT NOT NULL,
                months INTEGER DEFAULT 1,
                amount REAL NOT NULL,
                points_used INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                created_time TEXT,
                paid_time TEXT
            );
            CREATE TABLE IF NOT EXISTS referrals (
                inviter_id TEXT NOT NULL,
                invitee_id TEXT NOT NULL,
                rewarded INTEGER DEFAULT 0,
                reward_points INTEGER DEFAULT 0,
                created_time TEXT,
                PRIMARY KEY (inviter_id, invitee_id)
            );
            CREATE TABLE IF NOT EXISTS coupons (
                code TEXT PRIMARY KEY,
                discount_percent INTEGER NOT NULL DEFAULT 100,
                max_uses INTEGER NOT NULL DEFAULT 1,
                used_count INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT,
                created_time TEXT
            );
            CREATE TABLE IF NOT EXISTS coupon_redemptions (
                code TEXT NOT NULL,
                user_id TEXT NOT NULL,
                order_id TEXT NOT NULL,
                created_time TEXT,
                PRIMARY KEY (code, user_id)
            );
            """
        )
        for col, ddl in (("api_token", "ALTER TABLE users ADD COLUMN api_token TEXT"),):
            try:
                conn.execute(ddl)
            except Exception:
                pass  # column exists


def _ensure_columns() -> None:
    """老库迁移：users 表补 avatar_url 列。"""
    with store._conn() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "avatar_url" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN avatar_url TEXT DEFAULT ''")


_ensure_tables()
_ensure_columns()


def register(email: str, password: str, user_nick: str = "", invite_code: str = "") -> dict:
    if not email or "@" not in email:
        raise ValueError("邮箱格式不正确")
    if len(password) < 8:
        raise ValueError("密码至少 8 位")
    with store._conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            raise ValueError("邮箱已注册")
        uid = secrets.token_hex(8)
        inviter_id = None
        if invite_code:
            inviter = conn.execute(
                "SELECT id FROM users WHERE invite_code = ?", (invite_code.strip(),)
            ).fetchone()
            if inviter:
                inviter_id = inviter["id"]
        conn.execute(
            "INSERT INTO users (id, email, password_hash, user_nick, invite_code, created_time, updated_time)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uid, email, auth.hash_password(password), user_nick or email.split("@")[0], secrets.token_hex(4), _now(), _now()),
        )
        if inviter_id:
            # Self-invite is impossible at registration time (new user has no
            # invitee relations yet), but guard anyway against re-binding.
            conn.execute(
                "INSERT INTO referrals (inviter_id, invitee_id, created_time) VALUES (?, ?, ?)",
                (inviter_id, uid, _now()),
            )
    return login(email, password)


def login(email: str, password: str) -> dict:
    with store._conn() as conn:
        row = conn.execute(
            "SELECT id, email, password_hash, user_nick, avatar_url, role, points FROM users WHERE email = ?",
            (email,),
        ).fetchone()
    if not row or not auth.verify_password(password, row["password_hash"]):
        raise ValueError("邮箱或密码错误")
    user = _user_view(row)
    tokens = {
        "token": auth.issue_token(row["id"], "access"),
        "refresh_token": auth.issue_token(row["id"], "refresh"),
    }
    return {"user_info": user, **tokens}


def refresh(refresh_token: str) -> dict:
    payload = auth.verify_token(refresh_token, "refresh")
    if not payload:
        raise ValueError("登录已过期，请重新登录")
    uid = payload["sub"]
    row = _get_user(uid)
    if not row:
        raise ValueError("用户不存在")
    return {
        "token": auth.issue_token(uid, "access"),
        "refresh_token": auth.issue_token(uid, "refresh"),
        "user_info": _user_view(row),
    }


def _get_user(uid: str) -> sqlite3.Row | None:
    with store._conn() as conn:
        return conn.execute(
            "SELECT id, email, password_hash, user_nick, avatar_url, role, points FROM users WHERE id = ?",
            (uid,),
        ).fetchone()


def _user_view(row: sqlite3.Row | dict) -> dict:
    try:
        keys = row.keys() if hasattr(row, "keys") else []
        avatar = row["avatar_url"] if "avatar_url" in keys else ""
    except Exception:
        avatar = ""
    return {
        "id": row["id"],
        "email": row["email"],
        "user_nick": row["user_nick"],
        "avatar_url": avatar,
        "role": row["role"],
        "points": row["points"],
    }


def profile(uid: str) -> dict:
    row = _get_user(uid)
    if not row:
        raise ValueError("用户不存在")
    view = _user_view(row)
    view["subscriptions"] = list_subscriptions(uid)
    return view


POINTS_PER_YUAN = 100
POINTS_MAX_DISCOUNT_FRACTION = 0.5


def create_order(uid: str, module_code: str, months: int = 1, coupon_code: str = "", use_points: bool = False) -> dict:
    mods = {m["code"]: m for m in subscription.modules()}
    if module_code not in mods:
        raise ValueError("未知的订阅模块")
    months = max(1, min(int(months or 1), 12))
    amount = float(mods[module_code]["price_per_month"]) * months
    coupon_info = validate_coupon(uid, coupon_code) if coupon_code else None
    if coupon_info and coupon_info["valid"]:
        amount = round(amount * coupon_info["discount_percent"] / 100, 2)
    # Points redemption: 100 points = 1 CNY, capped at 50% of the order.
    points_used = 0
    if use_points and amount > 0:
        with store._conn() as conn:
            balance = conn.execute("SELECT points FROM users WHERE id = ?", (uid,)).fetchone()
        balance = balance["points"] if balance else 0
        max_yuan = round(amount * POINTS_MAX_DISCOUNT_FRACTION, 2)
        points_cap = int(max_yuan * POINTS_PER_YUAN)
        points_used = max(0, min(balance, points_cap))
        amount = round(amount - points_used / POINTS_PER_YUAN, 2)
    order_id = secrets.token_hex(16)
    with store._conn() as conn:
        conn.execute(
            "INSERT INTO orders (order_id, user_id, module_code, months, amount, points_used, status, created_time)"
            " VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (order_id, uid, module_code, months, amount, points_used, _now()),
        )
        if coupon_info and coupon_info["valid"]:
            # Bind the coupon to this order; redeemed when payment succeeds.
            # One redemption per user per coupon is enforced by the PK.
            conn.execute(
                "INSERT INTO coupon_redemptions (code, user_id, order_id, created_time) VALUES (?, ?, ?, ?)",
                (coupon_code.strip(), uid, order_id, _now()),
            )
    return {"order_id": order_id, "module_code": module_code, "months": months, "amount": amount, "status": "pending"}


def validate_coupon(uid: str, code: str) -> dict:
    """校验优惠券：存在、未过期、未用尽、该用户未核销过。"""
    code = (code or "").strip()
    if not code:
        return {"valid": False, "reason": "empty"}
    now = _now()
    with store._conn() as conn:
        c = conn.execute("SELECT * FROM coupons WHERE code = ?", (code,)).fetchone()
        if not c:
            return {"valid": False, "reason": "不存在"}
        if c["expires_at"] and c["expires_at"] < now:
            return {"valid": False, "reason": "已过期"}
        if c["used_count"] >= c["max_uses"]:
            return {"valid": False, "reason": "已用尽"}
        used = conn.execute(
            "SELECT 1 FROM coupon_redemptions WHERE code = ? AND user_id = ?", (code, uid)
        ).fetchone()
        if used:
            return {"valid": False, "reason": "已使用过"}
    return {"valid": True, "discount_percent": c["discount_percent"], "reason": ""}


def pay_order(uid: str, order_id: str) -> dict:
    """模拟支付成功回调（正式部署替换为微信支付回调验签后调用）。"""
    with store._conn() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE order_id = ? AND user_id = ?", (order_id, uid)
        ).fetchone()
        if not row:
            raise ValueError("订单不存在")
        if row["status"] == "paid":
            return {"order_id": order_id, "status": "paid", "already": True}
        now = datetime.now()
        conn.execute(
            "UPDATE orders SET status = 'paid', paid_time = ? WHERE order_id = ?",
            (_now(), order_id),
        )
        # Points redemption: deduct the points applied at order creation
        # only when payment actually succeeds.
        if row["points_used"]:
            conn.execute(
                "UPDATE users SET points = MAX(0, points - ?) WHERE id = ?",
                (row["points_used"], uid),
            )
        # Coupon redemption on successful payment: mark the coupon used once.
        coupon = conn.execute(
            "SELECT code FROM coupon_redemptions WHERE order_id = ? AND user_id = ?",
            (order_id, uid),
        ).fetchone()
        if coupon:
            conn.execute(
                "UPDATE coupons SET used_count = used_count + 1 WHERE code = ? AND used_count < max_uses",
                (coupon["code"],),
            )
        # Referral reward: first paid order from an invited user grants points
        # to the inviter (idempotent via rewarded flag).
        ref = conn.execute(
            "SELECT inviter_id, rewarded FROM referrals WHERE invitee_id = ?", (uid,)
        ).fetchone()
        if ref and not ref["rewarded"]:
            conn.execute(
                "UPDATE referrals SET rewarded = 1, reward_points = 20 WHERE invitee_id = ?",
                (uid,),
            )
            conn.execute(
                "UPDATE users SET points = points + 20 WHERE id = ?", (ref["inviter_id"],)
            )
        # 续费叠加：现有未过期订阅顺延
        cur = conn.execute(
            "SELECT expires_at FROM subscriptions WHERE user_id = ? AND module_code = ?",
            (uid, row["module_code"]),
        ).fetchone()
        base = datetime.fromisoformat(cur["expires_at"]) if cur and cur["expires_at"] else now
        if base < now:
            base = now
        expires = (base + timedelta(days=30 * row["months"])).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO subscriptions (user_id, module_code, expires_at) VALUES (?, ?, ?)"
            " ON CONFLICT(user_id, module_code) DO UPDATE SET expires_at = excluded.expires_at",
            (uid, row["module_code"], expires),
        )
    return {"order_id": order_id, "status": "paid", "expires_at": expires}


def list_orders(uid: str) -> list[dict]:
    with store._conn() as conn:
        rows = conn.execute(
            "SELECT order_id, module_code, months, amount, points_used, status, created_time, paid_time"
            " FROM orders WHERE user_id = ? ORDER BY created_time DESC",
            (uid,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_subscriptions(uid: str) -> list[dict]:
    now = _now()
    with store._conn() as conn:
        rows = conn.execute(
            "SELECT module_code, expires_at FROM subscriptions WHERE user_id = ?", (uid,),
        ).fetchall()
    active = []
    for r in rows:
        item = dict(r)
        item["active"] = bool(r["expires_at"] and r["expires_at"] > now)
        active.append(item)
    return active


def has_subscription(uid: str, module_code: str) -> bool:
    now = _now()
    with store._conn() as conn:
        row = conn.execute(
            "SELECT expires_at FROM subscriptions WHERE user_id = ? AND module_code = ?",
            (uid, module_code),
        ).fetchone()
    return bool(row and row["expires_at"] and row["expires_at"] > now)


TRIAL_LIMITS = {"pct_interval_vip": 3}


def consume_trial(uid: str, module_code: str) -> bool:
    """无订阅时的每日免费试用：限次内计数 +1 并放行，超限返回 False。

    对齐原站行为——ml 页涨幅区间模块每日前 3 次免费，之后 403 引导订阅。
    """
    from datetime import date
    limit = TRIAL_LIMITS.get(module_code, 0)
    if limit <= 0:
        return False
    day = date.today().isoformat()
    used = store.module_usage_get(uid, module_code, day)
    if used >= limit:
        return False
    store.module_usage_increment(uid, module_code, day)
    return True


def admin_list_users(page: int = 1, page_size: int = 20) -> dict:
    page, page_size = max(1, page), max(1, min(page_size, 100))
    with store._conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        rows = conn.execute(
            "SELECT id, email, user_nick, role, points, created_time FROM users"
            " ORDER BY created_time DESC LIMIT ? OFFSET ?",
            (page_size, (page - 1) * page_size),
        ).fetchall()
    items = []
    for r in rows:
        item = dict(r)
        item["subscriptions"] = list_subscriptions(r["id"])
        items.append(item)
    return {"total": total, "page": page, "page_size": page_size, "items": items}


def admin_set_role(uid: str, role: str) -> dict:
    if role not in ("user", "admin"):
        raise ValueError("role 仅支持 user/admin")
    with store._conn() as conn:
        cur = conn.execute("UPDATE users SET role = ?, updated_time = ? WHERE id = ?", (role, _now(), uid))
        if cur.rowcount == 0:
            raise ValueError("用户不存在")
    return {"uid": uid, "role": role}


def admin_grant_subscription(uid: str, module_code: str, months: int = 1) -> dict:
    mods = {m["code"] for m in subscription.modules()}
    if module_code not in mods:
        raise ValueError("未知的订阅模块")
    if not _get_user(uid):
        raise ValueError("用户不存在")
    months = max(1, min(int(months or 1), 36))
    now = datetime.now()
    with store._conn() as conn:
        cur = conn.execute(
            "SELECT expires_at FROM subscriptions WHERE user_id = ? AND module_code = ?",
            (uid, module_code),
        ).fetchone()
        base = datetime.fromisoformat(cur["expires_at"]) if cur and cur["expires_at"] else now
        if base < now:
            base = now
        expires = (base + timedelta(days=30 * months)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO subscriptions (user_id, module_code, expires_at) VALUES (?, ?, ?)"
            " ON CONFLICT(user_id, module_code) DO UPDATE SET expires_at = excluded.expires_at",
            (uid, module_code, expires),
        )
    return {"user_id": uid, "module_code": module_code, "expires_at": expires}


def admin_revoke_subscription(uid: str, module_code: str) -> dict:
    with store._conn() as conn:
        conn.execute(
            "DELETE FROM subscriptions WHERE user_id = ? AND module_code = ?",
            (uid, module_code),
        )
    return {"user_id": uid, "module_code": module_code, "revoked": True}


def admin_list_orders(status: str = "", page: int = 1, page_size: int = 20) -> dict:
    page, page_size = max(1, page), max(1, min(page_size, 100))
    where, args = "", []
    if status:
        where = " WHERE o.status = ?"
        args = [status]
    with store._conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM orders o" + where, args).fetchone()[0]
        rows = conn.execute(
            "SELECT o.order_id, o.user_id, u.email, o.module_code, o.months, o.amount, o.status,"
            " o.created_time, o.paid_time FROM orders o LEFT JOIN users u ON u.id = o.user_id"
            + where + " ORDER BY o.created_time DESC LIMIT ? OFFSET ?",
            [*args, page_size, (page - 1) * page_size],
        ).fetchall()
    return {"total": total, "page": page, "page_size": page_size, "items": [dict(r) for r in rows]}


def referral_stats(uid: str) -> dict:
    with store._conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(reward_points), 0) AS points"
            " FROM referrals WHERE inviter_id = ?",
            (uid,),
        ).fetchone()
        user = conn.execute("SELECT invite_code FROM users WHERE id = ?", (uid,)).fetchone()
    return {
        "count": row["total"],
        "reward_points": row["points"],
        "invite_code": user["invite_code"] if user else "",
        "reward_per_order": 20,
    }


def referral_admin_list(page: int = 1, page_size: int = 20) -> dict:
    """邀请记录管理列表：referral 与用户名联查。"""
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 20), 100))
    with store._conn() as conn:
        rows = conn.execute(
            """
            SELECT r.inviter_id, r.invitee_id, r.rewarded, r.reward_points, r.created_time,
                   u1.user_nick AS referrer, u2.user_nick AS referee
            FROM referrals r
            LEFT JOIN users u1 ON u1.id = r.inviter_id
            LEFT JOIN users u2 ON u2.id = r.invitee_id
            ORDER BY r.created_time DESC
            """
        ).fetchall()
    items = []
    for i, r in enumerate(rows):
        items.append({
            "id": i + 1,
            "record_id": f"{r['inviter_id']}:{r['invitee_id']}",
            "referrer": r["referrer"] or r["inviter_id"],
            "referee": r["referee"] or r["invitee_id"],
            "points": int(r["reward_points"] or 0),
            "status": "已奖励" if r["rewarded"] else "待审核",
            "created_at": r["created_time"] or "",
            "rewarded": int(r["rewarded"] or 0),
        })
    total = len(items)
    start = (page - 1) * page_size
    return {"items": items[start:start + page_size], "total": total, "page": page, "page_size": page_size}


def referral_audit(record_id: str, is_pass: bool) -> dict:
    """邀请审核：通过则发放积分，拒绝则置 0。"""
    if ":" not in (record_id or ""):
        raise ValueError("无效的记录 ID")
    inviter_id, invitee_id = record_id.split(":", 1)
    with store._conn() as conn:
        row = conn.execute(
            "SELECT rewarded FROM referrals WHERE inviter_id=? AND invitee_id=?",
            (inviter_id, invitee_id),
        ).fetchone()
        if not row:
            raise ValueError("记录不存在")
        if is_pass:
            conn.execute(
                "UPDATE referrals SET rewarded=1, reward_points=20 WHERE inviter_id=? AND invitee_id=?",
                (inviter_id, invitee_id),
            )
            conn.execute(
                "UPDATE users SET points = points + 20 WHERE id=?", (inviter_id,)
            )
        else:
            conn.execute(
                "UPDATE referrals SET rewarded=0, reward_points=0 WHERE inviter_id=? AND invitee_id=?",
                (inviter_id, invitee_id),
            )
    return {"record_id": record_id, "is_pass": bool(is_pass), "reward_points": 20 if is_pass else 0}


def referral_set_suspicious(user_id: str, is_suspicious: int) -> dict:
    """标记邀请嫌疑人：0 正常 / 1 嫌疑人 / 2 白名单。"""
    is_suspicious = int(is_suspicious or 0)
    if is_suspicious not in (0, 1, 2):
        raise ValueError("无效状态")
    from app.core.store import store as _store
    marks = _store.kv_get("referral_suspicious", {}) or {}
    marks[str(user_id)] = is_suspicious
    _store.kv_set("referral_suspicious", marks)
    return {"user_id": user_id, "is_suspicious": is_suspicious}


def admin_create_coupon(discount_percent: int = 100, max_uses: int = 1, days_valid: int = 30, code: str = "") -> dict:
    """管理员发放优惠券。discount_percent 为支付百分比（如 80 = 8 折）。"""
    discount_percent = max(1, min(int(discount_percent or 100), 100))
    max_uses = max(1, min(int(max_uses or 1), 10000))
    code = (code or "").strip() or secrets.token_hex(4).upper()
    expires = (datetime.now() + timedelta(days=max(1, int(days_valid or 30)))).isoformat(timespec="seconds")
    with store._conn() as conn:
        try:
            conn.execute(
                "INSERT INTO coupons (code, discount_percent, max_uses, used_count, expires_at, created_time)"
                " VALUES (?, ?, ?, 0, ?, ?)",
                (code, discount_percent, max_uses, expires, _now()),
            )
        except sqlite3.IntegrityError:
            raise ValueError("优惠券码已存在")
    return {"code": code, "discount_percent": discount_percent, "max_uses": max_uses, "expires_at": expires}


def admin_list_coupons() -> list[dict]:
    with store._conn() as conn:
        rows = conn.execute(
            "SELECT code, discount_percent, max_uses, used_count, expires_at, created_time FROM coupons ORDER BY created_time DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def change_password(uid: str, old_password: str, new_password: str) -> dict:
    """修改密码（原站 POST /user/profile/password）。"""
    if len(new_password) < 8:
        raise ValueError("新密码至少 8 位")
    row = _get_user(uid)
    if not row:
        raise ValueError("用户不存在")
    if not auth.verify_password(old_password, row["password_hash"]):
        raise ValueError("原密码错误")
    with store._conn() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, updated_time = ? WHERE id = ?",
            (auth.hash_password(new_password), _now(), uid),
        )
    return {"status": "password_changed"}


def update_email(uid: str, email: str) -> dict:
    """换绑邮箱：新邮箱不能已被其他账号占用。"""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("请输入有效的邮箱地址")
    with store._conn() as conn:  # noqa: SLF001
        dup = conn.execute("SELECT id FROM users WHERE email = ? AND id != ?", (email, uid)).fetchone()
        if dup:
            raise ValueError("该邮箱已被其他账号绑定")
        conn.execute("UPDATE users SET email = ?, updated_time = ? WHERE id = ?", (email, _now(), uid))
        row = conn.execute(
            "SELECT id, email, password_hash, user_nick, avatar_url, role, points FROM users WHERE id = ?",
            (uid,),
        ).fetchone()
    return _user_view(row)


def set_avatar(uid: str, url: str) -> dict:
    """保存头像 URL（原站 POST /user/profile/avatar/upload 后调用）。"""
    if not url or len(url) > 500:
        raise ValueError("无效的头像地址")
    with store._conn() as conn:
        conn.execute(
            "UPDATE users SET avatar_url = ?, updated_time = ? WHERE id = ?",
            (url, _now(), uid),
        )
    return {"url": url, "user_info": _user_view(_get_user(uid))}



def api_token_get(uid: str) -> dict:
    """返回当前 API 令牌（无则现场生成并持久化）。"""
    import secrets as _secrets
    with store._conn() as conn:
        row = conn.execute("SELECT api_token FROM users WHERE id = ?", (uid,)).fetchone()
        token = row["api_token"] if row else None
        if not token:
            token = "zzq_" + _secrets.token_hex(24)
            conn.execute("UPDATE users SET api_token = ?, updated_time = ? WHERE id = ?",
                         (token, _now(), uid))
    return {"token": token}


def api_token_refresh(uid: str) -> dict:
    """刷新 API 令牌：旧令牌立即失效。"""
    import secrets as _secrets
    token = "zzq_" + _secrets.token_hex(24)
    with store._conn() as conn:
        conn.execute("UPDATE users SET api_token = ?, updated_time = ? WHERE id = ?",
                     (token, _now(), uid))
    return {"token": token}


def points_adjust(target_uid: str, amount: int, action: str = "add", remark: str = "") -> dict:
    """管理员调整积分。action: add/reward/deduct。"""
    if action not in ("add", "reward", "deduct"):
        raise ValueError("invalid action")
    delta = int(amount) if action != "deduct" else -int(amount)
    if delta == 0:
        raise ValueError("amount must be non-zero")
    with store._conn() as conn:
        row = conn.execute("SELECT points FROM users WHERE id = ?", (target_uid,)).fetchone()
        if not row:
            raise ValueError("user not found")
        new_points = row["points"] + delta
        if new_points < 0:
            raise ValueError("insufficient points")
        conn.execute("UPDATE users SET points = ?, updated_time = ? WHERE id = ?",
                     (new_points, _now(), target_uid))
    return {"user_id": target_uid, "action": action, "amount": abs(delta),
            "delta": delta, "points": new_points, "remark": remark}
