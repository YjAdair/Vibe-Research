"""THS (10jqka) board daily K-line via d.10jqka.com.cn quotebridge.

Independent vendor series: THS board points are NOT comparable with eastmoney
board points. Stored under a separate table and never merged into
board_daily_history (code namespaces differ: 881xxx/885xxx/886xxx vs BKxxxxxx).
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import date
from pathlib import Path

import httpx

CATALOG_PATH = Path(__file__).with_name('ths_board_catalog.json')
BASE = 'https://d.10jqka.com.cn/v4/line'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Referer': 'https://d.10jqka.com.cn/',
}

_last_ts = 0.0


def _parse_bar_fields(parts: list[object]) -> dict | None:
    if len(parts) < 7:
        return None
    values = ["" if part is None else str(part).strip() for part in parts[:7]]
    if any(value == '' for value in values):
        return None
    if not re.fullmatch(r'\d{8}', values[0]):
        return None
    try:
        d = date(int(values[0][:4]), int(values[0][4:6]), int(values[0][6:8])).isoformat()
        open_, high, low, close, volume, amount = (float(value) for value in values[1:])
    except (ValueError, IndexError):
        return None
    if not all(math.isfinite(value) for value in (open_, high, low, close, volume, amount)):
        return None
    if min(open_, high, low, close) <= 0:
        return None
    if high < low or not (low <= open_ <= high and low <= close <= high):
        return None
    if volume < 0 or amount < 0:
        return None
    return {
        'date': d,
        'open': open_,
        'high': high,
        'low': low,
        'close': close,
        'volume': volume,
        'amount': amount,
    }


def load_catalog() -> list[dict]:
    data = json.loads(CATALOG_PATH.read_text(encoding='utf-8'))
    return data.get('boards') or []


def _parse_bars(payload: str) -> list[dict]:
    bars = []
    for line in payload.split(';'):
        bar = _parse_bar_fields(line.split(','))
        if bar is not None:
            bars.append(bar)
    return bars


def _extract_js_payload(text: str) -> dict:
    m = re.search(r'\((\{.*\})\)\s*$', text.strip(), re.S)
    if not m:
        raise ValueError('unexpected THS line payload')
    return json.loads(m.group(1))


async def fetch_board_bars(code: str, years: list[int] | None = None, client: httpx.AsyncClient | None = None) -> dict:
    """Fetch daily bars for one THS board. Default: last.js (140 bars) + today.

    years: optional list to fetch full-year files (e.g. [2024, 2025]) for deeper history.
    """
    global _last_ts
    own = client is None
    cli = client or httpx.AsyncClient(timeout=10, headers=HEADERS)
    try:
        bars: list[dict] = []
        name = ''
        if years:
            for y in years:
                wait = _last_ts + 0.08 - asyncio.get_running_loop().time()
                if wait > 0:
                    await asyncio.sleep(wait)
                _last_ts = asyncio.get_running_loop().time()
                r = await cli.get(f'{BASE}/bk_{code}/01/{y}.js')
                if r.status_code == 200 and r.text.strip():
                    try:
                        data = _extract_js_payload(r.text)
                        got = _parse_bars(data.get('data') or '')
                        if got:
                            bars.extend(got)
                    except ValueError:
                        continue
        wait = _last_ts + 0.08 - asyncio.get_running_loop().time()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_ts = asyncio.get_running_loop().time()
        r = await cli.get(f'{BASE}/bk_{code}/01/last.js')
        if r.status_code != 200:
            raise ValueError(f'THS board {code} unavailable: HTTP {r.status_code}')
        data = _extract_js_payload(r.text)
        name = data.get('name') or ''
        got = _parse_bars(data.get('data') or '')
        if not got:
            raise ValueError(f'THS board {code} has no bars')
        bars.extend(got)
        # today.js refreshes after close; skip failures silently.
        r2 = await cli.get(f'{BASE}/bk_{code}/01/today.js')
        if r2.status_code == 200:
            try:
                td = _extract_js_payload(r2.text).get(f'bk_{code}') or {}
                fields = ('1', '7', '8', '9', '11', '13', '19')
                if all(field in td for field in fields):
                    bar = _parse_bar_fields([td[field] for field in fields])
                    if bar is not None:
                        bars.append(bar)
            except (AttributeError, ValueError, KeyError, TypeError):
                pass
        seen = {}
        for b in bars:
            seen[b['date']] = b
        out = sorted(seen.values(), key=lambda b: b['date'])
        return {'code': code, 'name': name, 'bars': out}
    finally:
        if own:
            await cli.aclose()
