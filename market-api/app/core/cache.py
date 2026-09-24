"""进程内 TTL 缓存。

生产环境可无缝替换为 Redis（接口保持一致），用于降低对上游免费数据源的
请求压力、平滑突发流量，并把「数据及时性」与「请求频控」解耦。
"""

from __future__ import annotations

import threading
import time
from typing import Any


class TTLCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires_at, value = item
            if time.monotonic() >= expires_at:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: int) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + ttl, value)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


cache = TTLCache()
