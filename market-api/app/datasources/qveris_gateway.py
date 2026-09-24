"""QVeris 官方 HTTPS 网关。

这里只执行已经写入 ``QVERIS_BINDINGS_PATH`` 的工具绑定。搜索和 inspect
供后续人工确定绑定使用；execute 不会自动发现、猜测工具或处理别名。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote

import httpx


BASE_URL = "https://qveris.ai/api/v1"


class QVerisUnavailable(RuntimeError):
    """当前后端没有可用的 QVeris 配置或绑定。"""


class QVerisGatewayError(RuntimeError):
    """QVeris 请求失败；错误信息不包含响应正文或密钥。"""


@dataclass(frozen=True)
class ToolBinding:
    capability: str
    tool_id: str
    search_id: str | None
    default_parameters: dict[str, Any]
    result_path: tuple[str, ...]


def _path_parts(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        parts = tuple(part for part in value.split(".") if part)
    elif isinstance(value, list) and all(isinstance(part, str) and part for part in value):
        parts = tuple(value)
    else:
        raise ValueError("result_path must be a dotted string or string list")
    if not parts:
        raise ValueError("result_path must not be empty")
    return parts


def load_bindings(path: str | os.PathLike[str] | None = None) -> dict[str, ToolBinding]:
    """加载精确 capability 白名单；不做别名、同名供应商或工具猜测。"""

    configured_path = path or os.environ.get("QVERIS_BINDINGS_PATH")
    if not configured_path:
        return {}
    try:
        raw = json.loads(Path(configured_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QVerisGatewayError("invalid QVeris bindings configuration") from exc
    if not isinstance(raw, dict):
        raise QVerisGatewayError("invalid QVeris bindings configuration")
    entries = raw.get("bindings", raw)
    if not isinstance(entries, dict):
        raise QVerisGatewayError("invalid QVeris bindings configuration")

    bindings: dict[str, ToolBinding] = {}
    for capability, item in entries.items():
        if not isinstance(capability, str) or not capability.strip() or not isinstance(item, dict):
            raise QVerisGatewayError("invalid QVeris binding entry")
        tool_id = item.get("tool_id")
        search_id = item.get("search_id")
        defaults = item.get("default_parameters", {})
        if not isinstance(tool_id, str) or not tool_id.strip():
            raise QVerisGatewayError("invalid QVeris binding tool_id")
        if search_id is not None and (not isinstance(search_id, str) or not search_id.strip()):
            raise QVerisGatewayError("invalid QVeris binding search_id")
        if not isinstance(defaults, dict):
            raise QVerisGatewayError("invalid QVeris binding default_parameters")
        try:
            result_path = _path_parts(item.get("result_path"))
        except ValueError as exc:
            raise QVerisGatewayError("invalid QVeris binding result_path") from exc
        bindings[capability] = ToolBinding(
            capability=capability,
            tool_id=tool_id.strip(),
            search_id=search_id.strip() if isinstance(search_id, str) else None,
            default_parameters=dict(defaults),
            result_path=result_path,
        )
    return bindings


def _extract_path(value: Any, path: Sequence[str]) -> Any:
    current = value
    for part in path:
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            raise QVerisGatewayError("configured QVeris result_path was not found")
    return current


class QVerisGateway:
    def __init__(
        self,
        bindings_path: str | os.PathLike[str] | None = None,
        *,
        timeout: float = 12.0,
    ) -> None:
        self._bindings_path = bindings_path
        self._timeout = timeout

    @staticmethod
    def _api_key() -> str:
        key = os.environ.get("QVERIS_API_KEY", "").strip()
        if not key:
            raise QVerisUnavailable("QVeris API key is unavailable")
        return key

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        key = self._api_key()
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(BASE_URL + path, headers=headers, json=body)
                response.raise_for_status()
                payload = response.json()
        except QVerisUnavailable:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise QVerisGatewayError("QVeris HTTPS request failed") from exc
        if not isinstance(payload, dict):
            raise QVerisGatewayError("QVeris returned an invalid response")
        return payload

    async def search(self, query: str, limit: int = 10) -> dict[str, Any]:
        """发现工具元数据；调用方应传英文能力描述。"""

        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty capability description")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        return await self._post("/search", {"query": query.strip(), "limit": limit})

    async def inspect(self, tool_ids: Sequence[str], search_id: str | None = None) -> dict[str, Any]:
        """检查已发现工具的参数和输出说明，不执行工具。"""

        if isinstance(tool_ids, (str, bytes)):
            raise ValueError("tool_ids must be a sequence of strings")
        ids = list(tool_ids)
        if not ids or any(not isinstance(tool_id, str) or not tool_id.strip() for tool_id in ids):
            raise ValueError("tool_ids must contain non-empty strings")
        body: dict[str, Any] = {"tool_ids": [tool_id.strip() for tool_id in ids]}
        if search_id is not None:
            if not isinstance(search_id, str) or not search_id.strip():
                raise ValueError("search_id must be a non-empty string")
            body["search_id"] = search_id.strip()
        return await self._post("/tools/by-ids", body)

    def binding(self, capability: str) -> ToolBinding:
        bindings = load_bindings(self._bindings_path)
        binding = bindings.get(capability)
        if binding is None:
            raise QVerisUnavailable("QVeris capability has no approved binding")
        return binding

    async def execute(
        self,
        capability: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        max_response_size: int = 20480,
    ) -> Any:
        """执行精确白名单工具；不会自动 discover 或替换 tool_id。"""

        binding = self.binding(capability)
        if parameters is not None and not isinstance(parameters, Mapping):
            raise ValueError("parameters must be a mapping")
        if not isinstance(max_response_size, int) or max_response_size <= 0:
            raise ValueError("max_response_size must be a positive integer")
        merged = dict(binding.default_parameters)
        if parameters:
            merged.update(parameters)
        body: dict[str, Any] = {
            "parameters": merged,
            "max_response_size": max_response_size,
        }
        if binding.search_id:
            body["search_id"] = binding.search_id
        path = "/tools/execute?tool_id=" + quote(binding.tool_id, safe="")
        payload = await self._post(path, body)
        if payload.get("success") is False:
            raise QVerisGatewayError("QVeris tool execution failed")
        result = payload.get("result", payload)
        return _extract_path(result, binding.result_path) if binding.result_path else result
