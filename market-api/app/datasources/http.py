"""共享异步 HTTP 客户端（含重试、超时、UA）。"""

from __future__ import annotations

import httpx

from app.config import settings


_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=settings.http_timeout,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        )
    return _client


async def fetch_json(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    method: str = "GET",
    json: dict | None = None,
):
    client = get_client()
    if method.upper() == "POST":
        r = await client.post(url, params=params, headers=headers, json=json)
    else:
        r = await client.get(url, params=params, headers=headers)
    r.raise_for_status()
    return r.json()


async def fetch_form(url: str, data: dict, *, headers: dict | None = None, params: dict | None = None):
    """POST application/x-www-form-urlencoded and parse JSON."""
    client = get_client()
    r = await client.post(url, params=params, data=data, headers=headers)
    r.raise_for_status()
    return r.json()


async def fetch_text(url: str, *, params: dict | None = None, headers: dict | None = None):
    client = get_client()
    r = await client.get(url, params=params, headers=headers)
    r.raise_for_status()
    return r
