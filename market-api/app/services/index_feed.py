"""Published home-index quotes and minute trends."""
from __future__ import annotations
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from app.core.store import store
from app.datasources import tencent
from app.services.market import HOME_INDICES

TZ = ZoneInfo('Asia/Shanghai')


async def collect(day: str | None = None) -> dict:
    now = datetime.now(TZ)
    date = day or now.date().isoformat()
    symbols = [i['symbol'] for i in HOME_INDICES]
    quotes = await tencent.realtime_by_symbol(symbols)
    items = []
    for idx in HOME_INDICES:
        code6 = idx['symbol'][2:]
        q = quotes.get(code6) or {}
        stamp = str(q.get('timestamp') or '')
        if len(stamp) < 8 or stamp[:8] != date.replace('-', ''):
            raise ValueError(f'Index quote date mismatch for {idx["symbol"]}')
        try:
            session = await tencent.trend_minute_by_symbol(idx['symbol'])
        except Exception as exc:
            raise ValueError(f'Index trend unavailable for {idx["symbol"]}') from exc
        items.append({
            'code': idx['code'],
            'symbol': idx['symbol'],
            'name': idx['name'] or q.get('name') or idx['symbol'],
            'last_px': q.get('price'),
            'preclose_px': q.get('prev_close'),
            'open_px': q.get('open'),
            'high_px': q.get('high'),
            'low_px': q.get('low'),
            'px_change': q.get('change'),
            'px_change_rate': q.get('pct'),
            'source_as_of': stamp,
            'trend': [{'time': t, 'price': p} for t, p, *_ in session],
        })
    if len(items) != len(HOME_INDICES):
        raise ValueError('Incomplete home index batch')
    payload = {
        'date': date,
        'slot': now.strftime('%H:%M'),
        'complete': True,
        'total': len(items),
        'items': items,
        'source': 'tencent_public_quote',
        'collected_at': now.isoformat(),
    }
    store.index_snapshot_save(date, payload['slot'], payload)
    return payload


def published(day: str | None = None, *, allow_stale: bool = False) -> dict | None:
    if day:
        hit = store.index_snapshot_get(day)
        if hit or not allow_stale:
            return hit
        return store.index_snapshot_latest(day)
    return store.index_snapshot_latest()


async def review(day: str | None = None) -> list[dict]:
    snap = published(day, allow_stale=not bool(day))
    if snap:
        return [{**item, 'stale': bool(day is None and snap.get('date') != datetime.now(TZ).date().isoformat()),
                 'snapshot_date': snap.get('date'), 'snapshot_slot': snap.get('slot') } for item in snap['items']]
    if settings.collector_mode == 'embedded':
        snap = await collect(day or datetime.now(TZ).date().isoformat())
        return snap['items']
    return []
