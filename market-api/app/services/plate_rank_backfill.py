"""Resumable plate rank history backfill. Run: python -m app.services.plate_rank_backfill

Fetches origin plate daily rankings for plate types 17 (default), optionally 15/14,
walking backwards trade days from --end (default today). Each plate type & date is
one HTTP call throttled to the origin rate limit (10 req/min), so a full day set
of 3 types takes ~20s; typical runs are bounded via --days.

Resumability: dates already present in plate_rank_daily are skipped. Empty
responses (origin has no data for that date) are recorded in kv checkpoints so
re-runs do not hammer the endpoint.

--exact-only walks dates that already have snapshots but lack the full-precision
money_leader_exact column (origin rank/days n_days=1), also resumable via kv
checkpoint markers (plate_rank_exact_done:{pt}:{date}).
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from app.core.store import store

TZ = timezone(timedelta(hours=8))


def _trade_days(end: str, days: int) -> list[str]:
    """Recent dates descending, weekdays only (origin holds data only for sessions)."""
    d = datetime.fromisoformat(end).date()
    out = []
    while len(out) < days:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out


async def backfill(plate_types: list[int], end: str, days: int) -> dict:
    from app.datasources import zizizaizai
    report = {'saved': {}, 'skipped': {}, 'empty': [], 'errors': [], 'end': end, 'days': days}
    for plate_type in plate_types:
        existing = set(store.plate_rank_dates(plate_type, limit=1000))
        saved = skipped = 0
        for date in _trade_days(end, days):
            if date in existing:
                skipped += 1
                continue
            try:
                rows = await zizizaizai.plates_rank(plate_type, date, limit=500)
            except Exception as exc:  # noqa: BLE001 - keep walking, report at end
                report['errors'].append({'plate_type': plate_type, 'date': date, 'error': str(exc)[:200]})
                continue
            if not rows:
                report['empty'].append({'plate_type': plate_type, 'date': date})
                continue
            saved += store.plate_rank_save(plate_type, date, rows)
            existing.add(date)
            try:
                exact_rows = await zizizaizai.plates_rank_days(plate_type, date, n_days=1, limit=500)
                exact = {r['plate_code']: float(r['sum_leader_money']) for r in exact_rows
                         if r.get('plate_code') and isinstance(r.get('sum_leader_money'), (int, float))}
                store.plate_rank_exact_save(plate_type, date, exact)
                store.kv_set(f'plate_rank_exact_done:{plate_type}:{date}', 1)
            except Exception as exc:  # noqa: BLE001 - exact is a refinement, keep walking
                report['errors'].append({'plate_type': plate_type, 'date': date, 'step': 'exact', 'error': str(exc)[:200]})
        report['saved'][str(plate_type)] = saved
        report['skipped'][str(plate_type)] = skipped
    return report


async def backfill_exact(plate_types: list[int]) -> dict:
    """为已有快照但缺 money_leader_exact 的日期补全精度值（原站 rank/days n_days=1）。

    幂等可续传：kv done 标记的日期跳过；原站无数据的日期打标记防止反复请求。
    """
    from app.datasources import zizizaizai
    report = {'updated': {}, 'empty': [], 'errors': []}
    for plate_type in plate_types:
        updated = 0
        for date in store.plate_rank_exact_pending(plate_type):
            marker = f'plate_rank_exact_done:{plate_type}:{date}'
            try:
                rows = await zizizaizai.plates_rank_days(plate_type, date, n_days=1, limit=500)
            except Exception as exc:  # noqa: BLE001
                report['errors'].append({'plate_type': plate_type, 'date': date, 'error': str(exc)[:200]})
                continue
            if not rows:
                report['empty'].append({'plate_type': plate_type, 'date': date})
                store.kv_set(marker, 1)
                continue
            exact = {r['plate_code']: float(r['sum_leader_money']) for r in rows
                     if r.get('plate_code') and isinstance(r.get('sum_leader_money'), (int, float))}
            updated += store.plate_rank_exact_save(plate_type, date, exact)
            store.kv_set(marker, 1)
        report['updated'][str(plate_type)] = updated
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description='Backfill plate rank history')
    parser.add_argument('--types', default='17', help='comma separated plate types among 14,15,17')
    parser.add_argument('--end', default=datetime.now(TZ).date().isoformat(), help='end date YYYY-MM-DD')
    parser.add_argument('--days', type=int, default=5, help='number of trade dates to walk back')
    parser.add_argument('--exact-only', action='store_true',
                        help='only fill money_leader_exact for dates that already have snapshots')
    args = parser.parse_args()
    plate_types = [int(t) for t in args.types.split(',') if t.strip()]
    bad = [t for t in plate_types if t not in (14, 15, 17)]
    if bad:
        parser.error(f'unsupported plate types: {bad}')
    if args.exact_only:
        report = asyncio.run(backfill_exact(plate_types))
    else:
        report = asyncio.run(backfill(plate_types, args.end, args.days))
    import json
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
