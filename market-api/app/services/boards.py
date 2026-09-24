"""Board research read model, backed by complete dated provider snapshots.

Eastmoney board identifiers are preserved. They are not equivalent to the
reference site's proprietary 80xxxx topic taxonomy or its unpublished score.
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.cache import cache
from app.core.store import store
from app.datasources import eastmoney
from app.config import settings

TZ = ZoneInfo('Asia/Shanghai')
_locks: dict[int, asyncio.Lock] = {}


def iso_date(value: str) -> str:
    return datetime.strptime(value.replace('-', ''), '%Y%m%d').date().isoformat()


def board_type_for(plate_type: int) -> int:
    if plate_type in (17, 2):
        return 2
    if plate_type in (1, 3):
        return plate_type
    raise ValueError('Unsupported plate_type')


async def all_pages(fetch, key: str) -> list[dict]:
    """Stable code ordering; reject truncation/duplicates before publication."""
    first = await fetch(1)
    total = int(first['total'])
    if total <= 0 or not first[key]:
        raise ValueError('Provider returned an empty batch')
    page_size = len(first[key])
    gate = asyncio.Semaphore(4)

    async def page(n):
        async with gate:
            result = await fetch(n)
            if int(result['total']) != total:
                raise ValueError('Provider universe changed during pagination')
            return result[key]

    rest = await asyncio.gather(*(page(n) for n in range(2, math.ceil(total / page_size) + 1)))
    rows = first[key] + [r for group in rest for r in group]
    id_key = 'plate_code' if key == 'boards' else 'code'
    if len(rows) != total or len({r[id_key] for r in rows}) != total:
        raise ValueError('Incomplete or duplicate provider batch')
    return rows


async def collect(board_type: int) -> dict:
    cache_key = f'board_snapshot_v1:{board_type}'
    async with _locks.setdefault(board_type, asyncio.Lock()):
        hit = cache.get(cache_key)
        if hit is not None:
            return hit
        rows = await all_pages(
            lambda p: eastmoney.board_list(board_type, p, 100, sort='f12'), 'boards')
        times = [datetime.fromtimestamp(r['source_timestamp'], TZ) for r in rows if r.get('source_timestamp')]
        if len(times) != len(rows) or len({t.date() for t in times}) != 1:
            raise ValueError('Missing or mixed provider trading dates')
        trade_date = times[0].date().isoformat()
        now = datetime.now(TZ)
        if trade_date > now.date().isoformat():
            raise ValueError('Provider timestamp is in the future')
        meta = {
            'trade_date': trade_date, 'board_type': board_type, 'source': 'eastmoney',
            'source_as_of': min(times).isoformat(), 'collected_at': now.isoformat(),
            'count': len(rows), 'complete': True,
        }
        for r in rows:
            r.update(date1=trade_date, plate_type=board_type, source='eastmoney')
            r['rate'] = r['pct']
            r['trade_money'] = r['amount']
            r['money_leader'] = r['main_net_inflow']
            # Original score is not observable in this provider; do not fabricate it.
            r['score'] = None
        await asyncio.to_thread(store.board_snapshot_save, board_type, trade_date, rows, meta)
        cache.set(cache_key, meta, 60)
        return meta


async def evolution(plate_type: int, date1: str | None, days: int, limit: int, sort_by: str) -> dict:
    from app.services.market import trade_days

    board_type = board_type_for(plate_type)
    end = iso_date(date1) if date1 else datetime.now(TZ).date().isoformat()
    refresh_failed = False
    if settings.collector_mode == 'embedded' and end >= datetime.now(TZ).date().isoformat():
        try:
            await collect(board_type)
        except Exception:
            refresh_failed = True
    # Dates are obtained from an actual market series, never weekday arithmetic.
    published = store.kv_get('collector_calendar_v1',{}) if settings.collector_mode != 'embedded' else {}
    source_days = published.get('days') or await trade_days(250)
    calendar = sorted({iso_date(d) for d in source_days if iso_date(d) <= end}, reverse=True)[:days]
    if not calendar:
        return {'dates': [], 'columns': [], 'snapshots': [], 'status': 'missing', 'requested_date': end}
    rows, runs = await asyncio.to_thread(store.board_snapshot_range, board_type, calendar[-1], calendar[0])
    historical = await asyncio.to_thread(store.board_history_range, board_type, calendar[-1], calendar[0])
    merged = {(r['date1'], r['plate_code']): r for r in historical}
    for row in rows:
        key = (row['date1'], row['plate_code'])
        merged[key] = {**merged.get(key, {}), **row, 'data_kind': 'observed_snapshot'}
    rows = list(merged.values())
    by_day = {d: [] for d in calendar}
    for row in rows:
        if row['date1'] in by_day:
            by_day[row['date1']].append(row)
    fields = {'pct': 'pct', 'amount': 'amount', 'flow': 'main_net_inflow'}
    field = fields[sort_by]
    catalog = await asyncio.to_thread(store.kv_get, f'board_history_catalog:{board_type}', {})
    columns = []
    for day, items in by_day.items():
        eligible = [r for r in items if r.get(field) is not None]
        eligible.sort(key=lambda r: (r[field], r['plate_code']), reverse=True)
        columns.append({'date': day, 'total': len(items), 'ranked_count': len(eligible),
                        'missing_metric_count': len(items) - len(eligible),
                        'data_kind': 'observed_snapshot' if any(r.get('data_kind') == 'observed_snapshot' for r in items) else 'historical_daily',
                        'expected_current_catalog': catalog.get('expected'),
                        'status': ('partial' if len(eligible) < len(items) or catalog.get('expected', 0) > len(eligible) else 'available') if eligible else 'missing', 'items': eligible[:limit]})
    return {
        'dates': calendar, 'columns': columns, 'snapshots': runs,
        'status': 'refresh_failed' if refresh_failed else ('available' if rows else 'missing'),
        'requested_date': end, 'board_type': board_type, 'sort_by': sort_by,
        'refresh_mode':settings.collector_mode,
        'source': 'eastmoney', 'taxonomy': 'eastmoney_boards',
        'original_score_available': False,
        'history_universe_basis': 'current_board_catalog',
        'history_catalog': catalog,
        'history_backfill': await asyncio.to_thread(store.kv_get, f'board_history_report:{board_type}', None),
    }


async def history(plate_type: int, code: str, start: str, end: str) -> list[dict]:
    rows, _ = await asyncio.to_thread(store.board_snapshot_range, board_type_for(plate_type), iso_date(start), iso_date(end))
    historical = await asyncio.to_thread(store.board_history_range, board_type_for(plate_type), iso_date(start), iso_date(end))
    merged = {r['date1']: r for r in historical if r['plate_code'] == code}
    for row in rows:
        if row['plate_code'] == code:
            merged[row['date1']] = {**merged.get(row['date1'], {}), **row, 'data_kind': 'observed_snapshot'}
    if not merged:
        # 原站口径回退：板块级日度快照（score/money_leader/trade_money 等，与原站逐值一致）。
        try:
            legacy = await asyncio.to_thread(store.plate_rank_range, plate_type, iso_date(start), iso_date(end))
            for row in legacy:
                if row.get('plate_code') == code:
                    merged[row['date1']] = {**merged.get(row['date1'], {}), **row, 'data_kind': 'plate_rank_daily'}
        except ValueError:
            pass
    return sorted(merged.values(), key=lambda r: r['date1'])


async def members(code: str) -> dict:
    key = f'board_members_v1:{code}'
    hit = cache.get(key)
    if hit is not None:
        return hit
    rows = await all_pages(lambda p: eastmoney.board_stocks(code, p, 100, sort='f12'), 'stocks')
    rows.sort(key=lambda r: r.get('pct') or 0, reverse=True)
    stamps = [r['source_timestamp'] for r in rows if r.get('source_timestamp')]
    data = {'stocks': rows, 'total': len(rows), 'source': 'eastmoney',
            'source_as_of': datetime.fromtimestamp(min(stamps), TZ).isoformat() if stamps else None,
            'membership_basis': 'current_snapshot'}
    cache.set(key, data, 60)
    return data
