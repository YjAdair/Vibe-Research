#!/usr/bin/env python3
"""QVeris 后续能力发现/inspect CLI；只查元数据，绝不 execute。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.datasources.qveris_gateway import QVerisGateway  # noqa: E402


_SENSITIVE_NAMES = ("authorization", "api_key", "apikey", "token", "secret", "password")


def _safe_metadata(value: Any, api_key: str) -> Any:
    """只输出可供人工绑定的元数据，并递归去掉凭据字段。"""

    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key).lower()
            if any(part in name for part in _SENSITIVE_NAMES):
                continue
            safe[str(key)] = _safe_metadata(item, api_key)
        return safe
    if isinstance(value, list):
        return [_safe_metadata(item, api_key) for item in value]
    if isinstance(value, str) and api_key and api_key in value:
        return "[REDACTED]"
    return value


def _search_output(payload: Mapping[str, Any], api_key: str) -> dict[str, Any]:
    results = payload.get("results", [])
    if not isinstance(results, list):
        results = []
    selected = []
    for item in results:
        if isinstance(item, Mapping):
            selected.append(
                _safe_metadata(
                    {
                        key: item.get(key)
                        for key in (
                            "tool_id",
                            "name",
                            "description",
                            "success_rate",
                            "avg_execution_time_ms",
                            "parameters",
                            "params",
                            "billing_rule",
                            "expected_cost",
                        )
                        if key in item
                    },
                    api_key,
                )
            )
    return {"search_id": _safe_metadata(payload.get("search_id"), api_key), "results": selected}


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    gateway = QVerisGateway(bindings_path=args.bindings_path)
    api_key = os.environ.get("QVERIS_API_KEY", "")
    if args.command == "search":
        return _search_output(await gateway.search(args.query, args.limit), api_key)
    payload = await gateway.inspect(args.tool_id, args.search_id)
    return _safe_metadata(payload, api_key)


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover or inspect QVeris tools; never execute.")
    parser.add_argument("--bindings-path", default=None, help="optional QVERIS_BINDINGS_PATH override")
    subparsers = parser.add_subparsers(dest="command", required=True)
    search = subparsers.add_parser("search", help="search tool metadata")
    search.add_argument("query", help="English capability description")
    search.add_argument("--limit", type=int, default=10)
    inspect = subparsers.add_parser("inspect", help="inspect tool metadata")
    inspect.add_argument("tool_id", nargs="+" )
    inspect.add_argument("--search-id", default=None)
    args = parser.parse_args()
    try:
        output = asyncio.run(_run(args))
    except Exception as exc:  # pragma: no cover - CLI boundary
        print(f"qveris_discover: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
