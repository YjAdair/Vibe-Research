"""统一响应封装。"""

from __future__ import annotations

from typing import Any


OK = 20000
PARAM_ERROR = 50000
SERVER_ERROR = 50001


def ok(data: Any = None, message: str = "success") -> dict:
    return {"code": OK, "message": message, "data": data}


def fail(code: int, data: Any = None, message: str = "") -> dict:
    return {"code": code, "message": message, "data": data}
