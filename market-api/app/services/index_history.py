"""Benchmark index daily closes used by abnormal-move calculations."""
from __future__ import annotations
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from app.core.store import store
from app.datasources import tencent

TZ = ZoneInfo('Asia/Shanghai')
INDEX_SET = [
    {'code': '000002', 'symbol': 'sh000002', 'name': '上证A指', 'boards': ('sh_main',)},
    {'code': '399107', 'symbol': 'sz399107', 'name': '深证A指', 'boards': ('sz_main',)},
    {'code': '399102', 'symbol': 'sz399102', 'name': '创业板综', 'boards': ('cyb',)},
    {'code': '000680', 'symbol': 'sh000680', 'name': '科创综指', 'boards': ('kcb',)},
    {'code': '899050', 'symbol': 'bj899050', 'name': '北证50', 'boards': ('bj',)},
]
REQUIRED = {item['code'] for item in INDEX_SET if item['code'] != '899050'}


def iso(day: str) -> str:
    d = day.replace('-', '')
    return f'{d[:4]}-{d[4:6]}-{d[6:]}'


def parse_kline(symbol: str, data: dict, end: str) -> list[dict]:
    xs, ys = data.get('x') or [], data.get('y') or []
    if not xs or len(xs) != len(ys):
        raise ValueError(f'Incomplete index kline for {symbol}')
    rows, seen = [], set()
    for x, y in zip(xs, ys):
        if not isinstance(x, str) or len(x) != 8 or not x.isdigit() or len(y) < 5:
            raise ValueError(f'Invalid index bar for {symbol}')
        day = iso(x)
        if day in seen:
            raise ValueError(f'Duplicate index date for {symbol}')
        seen.add(day)
        open_px, close_px, high, low = (float(y[0]), float(y[1]), float(y[2]), float(y[3]))
        if min(open_px, close_px, high, low) <= 0 or not low <= min(open_px, close_px) <= max(open_px, close_px) <= high:
            raise ValueError(f'Invalid index OHLC for {symbol} {day}')
        if day <= end:
            rows.append({'trade_date': day, 'open': open_px, 'close': close_px, 'high': high, 'low': low})
    if not rows:
        raise ValueError(f'No index bars on or before {end} for {symbol}')
    if rows[-1]['trade_date'] != end:
        raise ValueError(f'Index {symbol} missing requested close {end}')
    return rows


async def collect(day: str, n: int = 80) -> dict:
    date = iso(day)
    series = {}
    for item in INDEX_SET:
        try:
            raw = await tencent.kline_day_by_symbol(item['symbol'], n)
            bars = parse_kline(item['symbol'], raw, date)
        except Exception as exc:
            if item['code'] == '899050':
                continue
            raise ValueError(f'Index history unavailable for {item["symbol"]}') from exc
        series[item['code']] = {
            'code': item['code'], 'symbol': item['symbol'], 'name': item['name'],
            'boards': list(item['boards']), 'bars': bars,
        }
    missing = REQUIRED - set(series)
    if missing:
        raise ValueError(f'Missing required index history: {sorted(missing)}')
    payload = {
        'date': date, 'complete': True, 'total': len(series), 'series': series,
        'source': 'tencent_index_kline', 'collected_at': datetime.now(TZ).isoformat(),
        'coverage': sorted(series),
        'note': '北证50 history is optional until the public kline window covers the lookback',
    }
    store.index_history_save(date, payload)
    return payload


def published(day: str) -> dict | None:
    return store.index_history_get(iso(day))


async def require(day: str) -> dict:
    hit = published(day)
    if hit:
        return hit
    if settings.collector_mode == 'embedded':
        return await collect(day)
    raise ValueError('Index history snapshot missing')
