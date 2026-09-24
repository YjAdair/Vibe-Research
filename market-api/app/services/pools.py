"""Dated limit-up / limit-down / broken-board snapshots for read models."""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from app.core.store import store
from app.datasources import eastmoney, ths

TZ = ZoneInfo('Asia/Shanghai')
_locks: dict[tuple[str, str], asyncio.Lock] = {}


def compact(day: str) -> str:
    return day.replace('-', '')


def iso(day: str) -> str:
    d = compact(day)
    return f'{d[:4]}-{d[4:6]}-{d[6:]}'


def _codes(rows: list[dict], key: str) -> list[str]:
    out = []
    for row in rows:
        code = str(row.get(key) or '')
        if len(code) != 6 or not code.isdigit():
            raise ValueError('Invalid stock code in pool')
        out.append(code)
    if len(set(out)) != len(out):
        raise ValueError('Duplicate stock in pool')
    return out


async def _all_ths(date: str, fetch) -> dict:
    first = await fetch(date, page=1, size=100)
    total = int(first.get('total') or 0)
    rows = list(first.get('pool') or [])
    if total < 0:
        raise ValueError('Invalid THS pool total')
    page = 2
    while len(rows) < total:
        nxt = await fetch(date, page=page, size=100)
        if int(nxt.get('total') or 0) != total:
            raise ValueError('THS pool universe changed during pagination')
        chunk = nxt.get('pool') or []
        if not chunk:
            raise ValueError('THS pool truncated')
        rows.extend(chunk)
        page += 1
        if page > 20:
            raise ValueError('THS pool pagination exceeded bound')
    return {'total': total, 'pool': rows}


async def collect_eastmoney(kind: str, day: str) -> dict:
    date = compact(day)
    fetch = {'up': eastmoney.limit_up_pool, 'down': eastmoney.limit_down_pool, 'broken': eastmoney.limit_broken_pool}[kind]
    data = await fetch(date, page=0, size=500)
    rows = list(data.get('pool') or [])
    total = int(data.get('total') if data.get('total') is not None else -1)
    if total < 0 or len(rows) != total or str(data.get('qdate','')).replace('-','') != date:
        raise ValueError('Eastmoney pool truncated')
    codes = _codes(rows, 'c')
    concepts_by_code = {}
    if kind == 'up' and codes:
        from app.datasources.codes import market_of
        sem = asyncio.Semaphore(8)
        async def one(code: str) -> tuple[str, list[str]]:
            async with sem:
                mkt = 0 if market_of(code) == 'sz' else 1
                data = await eastmoney.stock_boards(code, mkt)
                names = [b.get('plate_name') for b in data.get('boards') or [] if b.get('plate_name')]
                if len(names) != len(set(names)):
                    raise ValueError('Duplicate concept membership')
                return code, names
        pairs = await asyncio.gather(*(one(c) for c in codes))
        concepts_by_code = dict(pairs)
        if len(concepts_by_code) != len(codes):
            raise ValueError('Incomplete concept membership batch')
    payload = {
        'pool_kind': kind,
        'source': 'eastmoney',
        'date': iso(date),
        'requested_date': iso(date),
        'provider_qdate': str(data.get('qdate') or ''),
        'total': len(rows),
        'pool': rows,
        'concepts_by_code': concepts_by_code,
        'complete': True,
        'collected_at': datetime.now(TZ).isoformat(),
        'empty_ok': True,
    }
    store.limit_pool_save(f'em_{kind}', payload['date'], payload)
    return payload


async def collect_ths(day: str) -> dict:
    date = compact(day)
    data = await _all_ths(date, ths.limit_up_pool)
    rows = data['pool']
    _codes(rows, 'code')
    if len(rows) != data['total']:
        raise ValueError('THS pool truncated')
    payload = {
        'pool_kind': 'up',
        'source': '10jqka',
        'date': iso(date),
        'requested_date': iso(date),
        'total': len(rows),
        'pool': rows,
        'complete': True,
        'collected_at': datetime.now(TZ).isoformat(),
        'empty_ok': True,
    }
    store.limit_pool_save('ths_up', payload['date'], payload)
    return payload


async def collect_ths_broken(day: str) -> dict:
    """同花顺炸板池；东财历史池不可用时供梯队回退。"""
    date = compact(day)
    data = await _all_ths(date, ths.open_limit_pool)
    rows = data['pool']
    _codes(rows, 'code')
    if len(rows) != data['total']:
        raise ValueError('THS broken pool truncated')
    payload = {
        'pool_kind': 'broken',
        'source': '10jqka',
        'date': iso(date),
        'requested_date': iso(date),
        'total': len(rows),
        'pool': rows,
        'complete': True,
        'collected_at': datetime.now(TZ).isoformat(),
        'empty_ok': True,
    }
    store.limit_pool_save('ths_broken', payload['date'], payload)
    return payload


def _hm_to_fbt(hm: str) -> int:
    parts = str(hm or '').split(':')
    try:
        hh, mm = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        return hh * 10000 + mm * 100
    except (TypeError, ValueError):
        return 93000


def _parse_high_days(high_days: str) -> tuple[int, int]:
    """'6天4板'/'首板' -> (days, ct)；解析失败 (0, 0)。"""
    text = str(high_days or '').strip()
    if not text or text == '首板':
        return 0, 0
    try:
        head = text.split('板')[0]
        days = int(head.split('天')[0])
        ct = int(head.split('天')[-1])
        return days, ct
    except (ValueError, IndexError):
        return 0, 0


def ths_row_as_em(row: dict, *, broken: bool = False) -> dict:
    """同花顺涨停/炸板行 → 东财池字段子集，供梯队 _stock 复用。"""
    days, ct = _parse_high_days(row.get('high_days') or '')
    return {
        'c': str(row.get('code') or ''),
        'n': row.get('name') or '',
        'lbc': 0 if broken else int(row.get('lbc') or (ct if days == ct and ct >= 1 else 1) or 1),
        'fbt': _hm_to_fbt(row.get('first_limit_up_time') or ''),
        # THS open_num=开板次数，≠东财 zbc；不映射，避免烂34 等噪声
        'zbc': 0,
        'zttj': {'days': days, 'ct': ct},
        'fund': 0,
        'amount': row.get('amount') or 0,
        'tshare': 0,
        'ltsz': 0,
        '_ths_limit_up_type': row.get('limit_up_type') or '',
        '_ths_high_days': row.get('high_days') or '',
    }


def ladder_pool_rows(day: str) -> tuple[list[dict], list[dict], str | None]:
    """梯队用涨停/炸板行：优先东财已发布，缺则同花顺回退。

    Returns (up_rows, broken_rows, source) source in {eastmoney,10jqka,None}.
    """
    em_up = published('up', day, allow_stale=False)
    if em_up is not None:
        br = published('broken', day, allow_stale=False)
        return list(em_up.get('pool') or []), list((br or {}).get('pool') or []), 'eastmoney'
    ths_up = published('ths_up', day, allow_stale=False)
    if ths_up is None:
        return [], [], None
    ths_br = published('ths_broken', day, allow_stale=False)
    return (
        [ths_row_as_em(r) for r in ths_up.get('pool') or []],
        [ths_row_as_em(r, broken=True) for r in (ths_br or {}).get('pool') or []],
        '10jqka',
    )


async def collect_block_top(day: str) -> dict:
    date = compact(day)
    items = await ths.block_top(date)
    payload = {
        'date': iso(date),
        'requested_date': iso(date),
        'complete': True,
        'total': len(items),
        'items': items,
        'source': '10jqka_block_top',
        'collected_at': datetime.now(TZ).isoformat(),
    }
    store.block_top_save(payload['date'], payload)
    return payload


async def collect_session(day: str) -> dict:
    key = ('session', iso(day))
    async with _locks.setdefault(key, asyncio.Lock()):
        em_up, em_down, em_broken, ths_up, ths_broken = await asyncio.gather(
            collect_eastmoney('up', day),
            collect_eastmoney('down', day),
            collect_eastmoney('broken', day),
            collect_ths(day),
            collect_ths_broken(day),
            return_exceptions=True,
        )
        errors = []
        out = {}
        for name, result in [
            ('em_up', em_up), ('em_down', em_down), ('em_broken', em_broken),
            ('ths_up', ths_up), ('ths_broken', ths_broken),
        ]:
            if isinstance(result, Exception):
                errors.append(name + ':' + type(result).__name__)
            else:
                out[name] = {'date': result['date'], 'total': result['total']}
        # 东财历史常不可用：只要同花顺涨停池成功即可发布，供全题材梯队按成分求交
        if 'em_up' not in out and 'ths_up' not in out:
            raise ValueError('Limit-up snapshot unavailable: ' + ','.join(errors))
        # 仅当东财涨停已成功时，才要求同批东财跌停/炸板齐套；否则允许 THS 独活
        if 'em_up' in out:
            missing_em = [e for e in errors if e.startswith(('em_down:', 'em_broken:'))]
            if missing_em:
                raise ValueError('Incomplete pool session: ' + ','.join(errors))
        out['errors'] = errors
        return {'date': iso(day), 'complete': True, **out}


async def ensure_pools_for_rank_days(plate_type: int = 17, limit: int = 40) -> dict:
    """按题材榜日期补齐 THS 涨停/炸板池（全题材共用，非单板补洞）。"""
    days = store.plate_rank_dates(plate_type, limit)
    filled, skipped, failed = [], [], []
    for d in days:
        need_up = published('ths_up', d) is None
        need_br = published('ths_broken', d) is None
        if not need_up and not need_br:
            skipped.append(d)
            continue
        try:
            if need_up:
                await collect_ths(d)
            if need_br:
                await collect_ths_broken(d)
            filled.append(d)
        except Exception as e:
            failed.append({'date': d, 'error': type(e).__name__ + ':' + str(e)})
        await asyncio.sleep(0.25)
    return {'days': days, 'filled': filled, 'skipped': skipped, 'failed': failed}


def published_block_top(day: str | None = None, *, allow_stale: bool = False) -> dict | None:
    if day:
        hit = store.block_top_get(iso(day))
        if hit or not allow_stale:
            return hit
        latest = store.block_top_get(iso(day))
        return latest
    # latest by scanning is not needed for reads that require exact date
    return None


def published(kind: str, day: str | None = None, *, allow_stale: bool = False) -> dict | None:
    pool = {
        'up': 'em_up', 'down': 'em_down', 'broken': 'em_broken',
        'ths_up': 'ths_up', 'ths_broken': 'ths_broken',
    }[kind]
    if day:
        hit = store.limit_pool_get(pool, iso(day))
        if hit or not allow_stale:
            return hit
        return store.limit_pool_latest(pool, iso(day))
    return store.limit_pool_latest(pool)


async def require(kind: str, day: str | None = None, *, allow_stale: bool = False) -> dict:
    hit = published(kind, day, allow_stale=allow_stale)
    if hit:
        return hit
    if settings.collector_mode == 'embedded':
        await collect_session(day or datetime.now(TZ).date().isoformat())
        hit = published(kind, day, allow_stale=allow_stale)
        if hit:
            return hit
    raise FileNotFoundError('Published pool snapshot is missing')
