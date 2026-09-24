"""Resumable THS board K-line backfill. Run: python -m app.services.ths_board_backfill.

Each run fetches last.js (140 bars incl. today) per board from the probe
catalog, upserts into ths_board_daily, and persists a checkpoint per board so
re-runs only refresh boards whose stored latest bar is older than the
provider's newest. Full-year deep history is opt-in via --years.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone, timedelta

import httpx

from app.core.store import store
from app.datasources.ths_board import HEADERS, fetch_board_bars, load_catalog

TZ = timezone(timedelta(hours=8))
REPORT_KEY = 'ths_board_history_report'


async def backfill(kind: str = 'all', years: list[int] | None = None, limit: int = 0, offset: int = 0, force: bool = False, workers: int = 4) -> dict:
    catalog = [b for b in load_catalog() if kind in ('all', b['kind'])]
    if offset:
        catalog = catalog[offset:]
    if limit:
        catalog = catalog[:limit]
    report = {'boards_total': len(catalog), 'saved': 0, 'skipped_fresh': 0,
              'errors': [], 'coverage_gaps': [], 'status': 'running', 'started_at': datetime.now(TZ).isoformat(),
              'years': years or [], 'kind': kind}
    sem = asyncio.Semaphore(max(1, min(workers, 8)))
    # 日历可包含未来日期，不能把最后一项当作已经收盘的交易日。
    now = datetime.now(TZ)
    today = now.date().isoformat()
    calendar = store.kv_get('collector_calendar_v1', {}) or {}
    known_days = sorted({str(d).replace('-', '') for d in calendar.get('days', [])
                         if len(str(d).replace('-', '')) == 8})
    completed = [d for d in known_days if d < now.strftime('%Y%m%d') or
                 (d == now.strftime('%Y%m%d') and now.hour >= 16)]
    latest_trade_day = (datetime.strptime(completed[-1], '%Y%m%d').date().isoformat()
                        if completed else None)
    report['expected_completed_day'] = latest_trade_day
    stats = {r['board_code']: r for r in store.ths_board_daily_stats()}
    async with httpx.AsyncClient(timeout=12, headers=HEADERS) as client:
        async def one(board: dict) -> None:
            code = board['code']
            async with sem:
                try:
                    if not force and not years:
                        cached = stats.get(code)
                        if cached and latest_trade_day and cached['latest'] == latest_trade_day and cached['latest'] < today:
                            report['skipped_fresh'] += 1
                            return
                    data = None
                    for attempt in range(3):
                        try:
                            data = await fetch_board_bars(code, years=years, client=client)
                            break
                        except ValueError:
                            raise
                        except Exception:  # noqa: BLE001 - transient 5xx / timeouts
                            if attempt == 2:
                                raise
                            await asyncio.sleep(1.5 * (attempt + 1))
                    # 不把供应商误报的未来日线存入研究库。
                    valid_bars = [b for b in data['bars'] if b['date'] <= today]
                    if not valid_bars:
                        raise ValueError('no bars at or before collection day')
                    n = store.ths_board_daily_save(code, valid_bars)
                    report['saved'] += 1
                    # 年度请求成功返回近期行情，不等于请求年份已有数据。
                    for year in years or []:
                        rows = store.ths_board_daily_range(code, f'{year}-01-01', f'{year}-12-31')
                        if not rows:
                            report['coverage_gaps'].append({'code': code, 'year': year,
                                                           'reason': 'requested_year_missing'})
                except Exception as exc:  # noqa: BLE001
                    report['errors'].append({'code': code, 'error': str(exc)[:160]})
        await asyncio.gather(*[one(b) for b in catalog])
    report['status'] = ('finished_with_errors' if report['errors'] else
                        'finished_with_gaps' if report['coverage_gaps'] else 'finished')
    report['finished_at'] = datetime.now(TZ).isoformat()
    store.kv_set(REPORT_KEY, report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--kind', default='all', choices=['all', 'industry', 'concept'])
    parser.add_argument('--years', default='', help='comma-separated years for deep history, e.g. 2024,2025')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    yrs = [int(y) for y in args.years.split(',') if y.strip()] if args.years else None
    result = asyncio.run(backfill(args.kind, yrs, args.limit, args.offset, args.force, args.workers))
    print({k: v for k, v in result.items() if k != 'errors'})
    for err in result['errors'][:10]:
        print('ERR', err)
