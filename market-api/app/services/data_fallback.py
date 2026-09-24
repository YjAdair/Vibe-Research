"""免费优先的数据源回退协调器。

调用方负责提供已经绑定业务参数的零参数回调：

    result = await resolve(
        "board_history",
        free_fetch=lambda: board_history.fetch_series(code, start, end),
        validate=is_valid_board_history,
        paid_fetch=lambda: qveris_gateway.execute(...),
        cache_key=f"board_history:{code}:{start}:{end}",
    )

``paid_fetch`` 只有在免费回调异常或校验失败、且付费开关、密钥和每日预算
均满足时才会被调用。验证器可以是普通函数或 async 函数；任何回调异常都按
失败处理，付费回调不会自动重试。
"""

from __future__ import annotations

import hashlib
import inspect
import os
import sqlite3
import time
from datetime import datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from app.core.store import Store


FREE_FAILURE_COOLDOWN_SECONDS = 30
_BUDGET_TABLE = "data_fallback_paid_budget"
_COOLDOWN_TABLE = "data_fallback_free_cooldown"
_store: Store | None = None

Callback = Callable[[], Any | Awaitable[Any]]


def configure_store(store: Store | None) -> None:
    """为进程设置持久化库；传入 ``None`` 可恢复为懒加载默认库。

    生产调用通常无需调用此函数。它也让嵌入式 worker 和测试可以明确使用
    自己的 SQLite 文件，而不改变全局 Store 实现。
    """

    global _store
    _store = store


def _get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def _today_shanghai() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _paid_config() -> tuple[bool, int | None]:
    """返回付费资格和每日上限；密钥只读取，不进入返回值或日志。"""

    if not _env_enabled("QVERIS_PAID_FALLBACK_ENABLED"):
        return False, None
    if not os.environ.get("QVERIS_API_KEY", "").strip():
        return False, None
    raw_limit = os.environ.get("QVERIS_PAID_DAILY_LIMIT", "").strip()
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return False, None
    if limit <= 0:
        return False, None
    return True, limit


def _key_digest(cache_key: str) -> str:
    return hashlib.sha256(cache_key.encode("utf-8")).hexdigest()


def _ensure_tables(store: Store) -> None:
    with store._conn() as conn:  # noqa: SLF001 - 复用项目已有连接边界
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {_BUDGET_TABLE} (
                day TEXT PRIMARY KEY,
                reserved_count INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {_COOLDOWN_TABLE} (
                cache_key_hash TEXT PRIMARY KEY,
                expires_at REAL NOT NULL
            )"""
        )


def _cooldown_active(store: Store, cache_key: str) -> bool:
    _ensure_tables(store)
    now = time.time()
    with store._conn() as conn:  # noqa: SLF001
        row = conn.execute(
            f"SELECT expires_at FROM {_COOLDOWN_TABLE} WHERE cache_key_hash = ?",
            (_key_digest(cache_key),),
        ).fetchone()
        if row is None:
            return False
        if float(row[0]) > now:
            return True
        conn.execute(
            f"DELETE FROM {_COOLDOWN_TABLE} WHERE cache_key_hash = ?",
            (_key_digest(cache_key),),
        )
        return False


def _remember_free_failure(store: Store, cache_key: str) -> None:
    _ensure_tables(store)
    expires_at = time.time() + FREE_FAILURE_COOLDOWN_SECONDS
    with store._conn() as conn:  # noqa: SLF001
        conn.execute(
            f"""INSERT INTO {_COOLDOWN_TABLE}(cache_key_hash, expires_at)
                VALUES (?, ?)
                ON CONFLICT(cache_key_hash) DO UPDATE SET expires_at = excluded.expires_at""",
            (_key_digest(cache_key), expires_at),
        )


def _reserve_paid_slot(store: Store, day: str, limit: int) -> bool:
    """在事务中预留一次付费调用；异常或失败调用也不会归还次数。"""

    _ensure_tables(store)
    with store._conn() as conn:  # noqa: SLF001
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT reserved_count FROM {_BUDGET_TABLE} WHERE day = ?", (day,)
            ).fetchone()
            used = int(row[0]) if row is not None else 0
            if used >= limit:
                conn.rollback()
                return False
            conn.execute(
                f"""INSERT INTO {_BUDGET_TABLE}(day, reserved_count) VALUES (?, 1)
                    ON CONFLICT(day) DO UPDATE SET reserved_count = reserved_count + 1""",
                (day,),
            )
            conn.commit()
            return True
        except sqlite3.Error:
            conn.rollback()
            return False


async def _invoke(callback: Callback, *args: Any) -> Any:
    value = callback(*args)
    if inspect.isawaitable(value):
        return await value
    return value


def _result(
    capability: str,
    *,
    ok: bool,
    source: str,
    data: Any = None,
    reason: str | None = None,
    paid_attempted: bool = False,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "capability": capability,
        "source": source,
        "data": data,
        "reason": reason,
        "paid_attempted": paid_attempted,
    }


async def resolve(
    capability: str,
    free_fetch: Callback,
    validate: Callback,
    paid_fetch: Callback | None = None,
    cache_key: str | None = None,
) -> dict[str, Any]:
    """先取免费数据，免费失败且具备显式资格时最多取一次付费数据。

    参数契约：``free_fetch``、``validate`` 和 ``paid_fetch`` 都是零参数闭包。
    ``validate(data)`` 返回真值才算合格，验证异常等同于不合格。返回字典的
    ``source`` 为 ``free``、``paid`` 或 ``unavailable``；合格数据在 ``data``。
    ``cache_key`` 仅用于免费失败的 30 秒短冷却，不缓存任何数据或不合格结果。
    """

    if not isinstance(capability, str) or not capability.strip():
        raise ValueError("capability must be a non-empty string")
    if not callable(free_fetch) or not callable(validate):
        raise TypeError("free_fetch and validate must be callable")
    if paid_fetch is not None and not callable(paid_fetch):
        raise TypeError("paid_fetch must be callable")

    store = _get_store()
    if cache_key is not None and _cooldown_active(store, cache_key):
        return _result(capability, ok=False, source="unavailable", reason="free_cooldown")

    free_ok = False
    free_data: Any = None
    try:
        free_data = await _invoke(free_fetch)
        free_ok = bool(await _invoke(validate, free_data))
    except Exception:
        free_ok = False

    if free_ok:
        return _result(capability, ok=True, source="free", data=free_data)

    if cache_key is not None:
        _remember_free_failure(store, cache_key)

    eligible, limit = _paid_config()
    if paid_fetch is None:
        return _result(capability, ok=False, source="unavailable", reason="paid_not_configured")
    if not eligible or limit is None:
        return _result(capability, ok=False, source="unavailable", reason="paid_disabled")
    if not _reserve_paid_slot(store, _today_shanghai(), limit):
        return _result(capability, ok=False, source="unavailable", reason="paid_budget_exhausted")

    try:
        paid_data = await _invoke(paid_fetch)
        paid_ok = bool(await _invoke(validate, paid_data))
    except Exception:
        return _result(
            capability,
            ok=False,
            source="unavailable",
            reason="paid_failed",
            paid_attempted=True,
        )
    if not paid_ok:
        return _result(
            capability,
            ok=False,
            source="unavailable",
            reason="paid_invalid",
            paid_attempted=True,
        )
    return _result(capability, ok=True, source="paid", data=paid_data, paid_attempted=True)

