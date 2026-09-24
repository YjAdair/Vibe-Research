"""Eastmoney board daily bars and independent daily fund-flow series.

多主机轮转 + Connection: close + 全局限频：东财对 keep-alive 连接与高频
请求会直接断连（RemoteProtocolError），实测教训见 eastmoney._kline_fetch。
"""
from __future__ import annotations

import asyncio
import math
from datetime import date

import httpx

from app.core.cache import TTLCache
from app.datasources.http import fetch_json

HOSTS = (
    'https://92.push2his.eastmoney.com',
    'https://push2his.eastmoney.com',
    'https://push2delay.eastmoney.com',
)
HEADERS = {'Referer': 'https://quote.eastmoney.com/', 'Connection': 'close'}

_last_request_ts = 0.0
_request_lock = asyncio.Lock()
_failure_cache = TTLCache()
_inflight: dict[str, asyncio.Task] = {}
FAILURE_COOLDOWN_SECONDS = 5 * 60
MAX_HOST_ATTEMPTS = 3
MIN_REQUEST_INTERVAL = 0.5


def number(value):
    if value in (None, '', '-'):
        return None
    n = float(value)
    if not math.isfinite(n):
        raise ValueError('Non-finite provider number')
    return n


def parse_series(data: dict, code: str, start: str, end: str, flow: bool = False) -> list[dict]:
    if not isinstance(data, dict):
        raise ValueError('Invalid board history response')
    payload = data.get('data')
    if data.get('rc') != 0 or not isinstance(payload, dict) or payload.get('code') != code:
        raise ValueError('Invalid board history response')
    values = payload.get('klines')
    if not isinstance(values, list):
        raise ValueError('Missing historical series')
    rows, seen = [], set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError('Unexpected historical kline type')
        parts = value.split(',')
        if len(parts) != (15 if flow else 11):
            raise ValueError('Unexpected historical field count')
        day = date.fromisoformat(parts[0]).isoformat()
        if day in seen:
            raise ValueError('Duplicate historical date')
        seen.add(day)
        if not start <= day <= end:
            continue
        if flow:
            rows.append({'date1': day, 'main_net_inflow': number(parts[1])})
        else:
            row = dict(zip(['open', 'price', 'high', 'low', 'volume', 'amount', 'amplitude', 'pct', 'change', 'turnover'], map(number, parts[1:])))
            if any(row[k] is None for k in ('open', 'price', 'high', 'low', 'amount', 'pct')):
                raise ValueError('Missing required OHLC field')
            if row['amount'] < 0 or not 0 < row['low'] <= min(row['open'], row['price']) <= max(row['open'], row['price']) <= row['high']:
                raise ValueError('Invalid historical OHLC range')
            rows.append({'date1': day, **row})
    return sorted(rows, key=lambda r: r['date1'])


async def fetch_series(code: str, start: str, end: str, flow: bool = False) -> list[dict]:
    cache_key = f'board_history:{code}:{start}:{end}:{int(flow)}'
    cached_failure = _failure_cache.get(cache_key)
    if cached_failure and cached_failure.get('cooldown'):
        raise cached_failure['error']

    task = _inflight.get(cache_key)
    if task is None:
        task = asyncio.create_task(_fetch_series_uncached(code, start, end, flow, cache_key))
        _inflight[cache_key] = task
    try:
        return await asyncio.shield(task)
    finally:
        if task.done() and _inflight.get(cache_key) is task:
            _inflight.pop(cache_key, None)


async def _fetch_series_uncached(
    code: str,
    start: str,
    end: str,
    flow: bool,
    cache_key: str,
) -> list[dict]:
    params = {'secid': '90.' + code, 'klt': 101, 'fields1': 'f1,f2,f3,f4,f5,f6',
              'fields2': ','.join('f' + str(n) for n in range(51, 66 if flow else 62)),
              'lmt': 180, 'fqt': 0, 'beg': start.replace('-', ''), 'end': end.replace('-', '')}
    path = 'fflow/daykline/get' if flow else 'kline/get'

    last_exc: Exception | None = None
    for host in HOSTS[:MAX_HOST_ATTEMPTS]:
        try:
            await _reserve_request_slot()
            data = await fetch_json(host + '/api/qt/stock/' + path, params=params, headers=HEADERS)
            rows = parse_series(data, code, start, end, flow)
            if rows:
                _failure_cache.delete(cache_key)
                return rows
            # rc=0 且序列为空或仅包含区间外数据：继续换主机确认，最多 3 次。
            empty_error = ValueError('No historical bars for board ' + code)
            _record_failure(cache_key, empty_error)
            if (_failure_cache.get(cache_key) or {}).get('cooldown'):
                last_exc = empty_error
                break
        except ValueError as exc:
            # 任意 schema/数据契约错误都在首个异常主机处早停，并进入冷却。
            error = _record_failure(cache_key, exc, cooldown=True)
            raise error
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            _record_failure(cache_key, exc)
            if (_failure_cache.get(cache_key) or {}).get('cooldown'):
                break
            continue

    error = last_exc or ValueError('No historical bars for board ' + code)
    raise error


async def _reserve_request_slot() -> None:
    """在锁内完成等待和时间预留，避免并发调用绕过全局限频。"""
    global _last_request_ts
    async with _request_lock:
        loop = asyncio.get_running_loop()
        wait = _last_request_ts + MIN_REQUEST_INTERVAL - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_ts = loop.time()


def _record_failure(cache_key: str, error: Exception, cooldown: bool = False) -> Exception:
    state = _failure_cache.get(cache_key) or {'count': 0}
    count = int(state.get('count', 0)) + 1
    _failure_cache.set(
        cache_key,
        {'count': count, 'cooldown': cooldown or count >= 3, 'error': error},
        FAILURE_COOLDOWN_SECONDS,
    )
    return error
