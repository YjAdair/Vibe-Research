"""Check QVeris key presence without printing secrets; inspect sector_rank meta if possible."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_env_files() -> None:
    for p in [
        ROOT / ".env",
        ROOT.parent / ".env",
        ROOT / ".env.local",
        Path.home() / ".zzquant.env",
    ]:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
        print("loaded_env_file", p)


def main() -> None:
    _load_env_files()
    key = os.environ.get("QVERIS_API_KEY", "").strip()
    paid = os.environ.get("QVERIS_PAID_FALLBACK_ENABLED", "").strip()
    limit = os.environ.get("QVERIS_PAID_DAILY_LIMIT", "").strip()
    print("QVERIS_API_KEY", "present" if key else "missing", "len", len(key))
    print("QVERIS_PAID_FALLBACK_ENABLED", repr(paid))
    print("QVERIS_PAID_DAILY_LIMIT", repr(limit))

    # metadata-only discover if key present
    if not key:
        print("skip_discover: no key")
        return
    try:
        from app.datasources.qveris_gateway import QVerisGateway
        import asyncio

        async def run():
            gw = QVerisGateway()
            # discover/search sector related
            tools = await gw.search("sector rank strength concept ifind", limit=10)
            print("search_hits", len(tools) if isinstance(tools, list) else type(tools))
            # print tool ids only
            if isinstance(tools, list):
                for t in tools[:8]:
                    if isinstance(t, dict):
                        print(" ", t.get("tool_id") or t.get("id") or t.get("name"))
                    else:
                        print(" ", t)

        asyncio.run(run())
    except Exception as exc:
        print("discover_err", type(exc).__name__, str(exc)[:200])


if __name__ == "__main__":
    main()
