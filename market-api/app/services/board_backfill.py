"""Resumable bounded history collection. Run: python -m app.services.board_backfill."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime

from app.core.store import store
from app.datasources.board_history import fetch_series
from app.services.boards import TZ, collect, iso_date


async def backfill(board_type: int, start: str, end: str, workers: int = 1, force: bool = False) -> dict:
    start, end = iso_date(start), iso_date(end)
    if start > end:
        raise ValueError('start must not exceed end')
    if not 1 <= workers <= 4:
        raise ValueError('workers must be between 1 and 4')
    if (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days > 180:
        raise ValueError('Backfill range must be at most 180 calendar days per run')
    meta = await collect(board_type)
    universe, _ = await asyncio.to_thread(store.board_snapshot_range, board_type, meta['trade_date'], meta['trade_date'])
    gate = asyncio.Semaphore(workers)
    report = {'type': board_type, 'start': start, 'end': end, 'universe_date': meta['trade_date'],
              'expected': len(universe), 'saved': 0, 'skipped': 0, 'errors': [], 'rows': 0,
              'status': 'running', 'started_at': datetime.now(TZ).isoformat()}
    report_key = f'board_history_report:{board_type}'
    await asyncio.to_thread(store.kv_set, report_key, report)
    await asyncio.to_thread(store.kv_set, f'board_history_catalog:{board_type}',
                            {'expected': len(universe), 'universe_date': meta['trade_date']})
    # Permanent skip list: provider confirmed these boards have no daily bars
    # (statistical boards). Persisted so every run does not re-probe them.
    skip_doc = await asyncio.to_thread(store.kv_get, f'board_history_skip:{board_type}') or {}
    skip_codes = skip_doc.get('codes') if isinstance(skip_doc, dict) else skip_doc
    skip_list = set(skip_codes or [])
    skip_added = []
    pending = []
    for board in universe:
        checkpoint = f'board_history_v1:{board_type}:{board["plate_code"]}:{start}:{end}'
        previous = await asyncio.to_thread(store.kv_get, checkpoint)
        if board['plate_code'] in skip_list:
            report['skipped'] += 1
        elif previous and previous.get('complete') and not force:
            report['skipped'] += 1
        else:
            pending.append(board)
    report['skip_list_size'] = len(skip_list)
    if not pending:
        report.update(status='finished', finished_at=datetime.now(TZ).isoformat())
        await asyncio.to_thread(store.kv_set, report_key, report)
        return report
    # Probe both services once before fan-out. A global transport outage should
    # not produce thousands of retries. Boards confirmed to have no series are
    # persisted to the skip list and skipped by the probe.
    probe = None
    while pending:
        candidate = pending[0]['plate_code']
        try:
            await fetch_series(candidate, start, end)
            await fetch_series(candidate, start, end, flow=True)
            probe = candidate
            break
        except Exception as exc:
            if 'No historical bars' in str(exc) or 'Missing historical series' in str(exc):
                skip_list.add(candidate)
                skip_added.append(candidate)
                await asyncio.to_thread(store.kv_set, f'board_history_skip:{board_type}',
                                        {'codes': sorted(skip_list), 'updated_at': datetime.now(TZ).isoformat(),
                                         'last_added': [candidate]})
                report['skipped'] += 1
                pending.pop(0)
                continue
            report.update(status='source_unavailable', finished_at=datetime.now(TZ).isoformat())
            report['errors'].append({'code': candidate, 'error': str(exc)[:160]})
            await asyncio.to_thread(store.kv_set, report_key, report)
            return report
    if probe is None:
        report.update(status='finished', skip_list_size=len(skip_list), finished_at=datetime.now(TZ).isoformat())
        if skip_added:
            report['skip_added'] = skip_added
        await asyncio.to_thread(store.kv_set, report_key, report)
        return report
    from app.services.market import trade_days
    calendar = {iso_date(d) for d in await trade_days(250) if start <= iso_date(d) <= end}
    if not calendar:
        report.update(status='calendar_unavailable', finished_at=datetime.now(TZ).isoformat())
        await asyncio.to_thread(store.kv_set, report_key, report)
        return report
    report['expected_trading_days'] = len(calendar)
    stop = asyncio.Event()
    consecutive_errors = 0

    async def one(board):
        nonlocal consecutive_errors
        code = board['plate_code']
        checkpoint = f'board_history_v1:{board_type}:{code}:{start}:{end}'
        async with gate:
            if stop.is_set():
                return
            try:
                # At most workers requests in flight. Funds and bars have independent schemas.
                bars = await fetch_series(code, start, end)
                if not bars:
                    raise ValueError('No historical bars in requested range')
                try:
                    flows = await fetch_series(code, start, end, flow=True)
                except Exception:
                    flows = []
                by_date = {r['date1']: r['main_net_inflow'] for r in flows}
                now = datetime.now(TZ).isoformat()
                for row in bars:
                    row.update(plate_code=code, plate_name=board['plate_name'], plate_type=board_type,
                               main_net_inflow=by_date.get(row['date1']), source='eastmoney',
                               data_kind='historical_daily', collected_at=now,
                               price_collected_at=now,
                               flow_collected_at=now if by_date.get(row['date1']) is not None else None,
                               universe_as_of=meta['trade_date'], name_basis='current_universe',
                               up_count=None, down_count=None, score=None)
                    row.update(rate=row['pct'], trade_money=row['amount'], money_leader=row['main_net_inflow'])
                # Empty/missing flows remain null and are retried on subsequent runs.
                complete = {r['date1'] for r in bars} == calendar and all(r['main_net_inflow'] is not None for r in bars)
                await asyncio.to_thread(store.board_history_save, board_type, code, bars)
                await asyncio.to_thread(store.kv_set, checkpoint, {'complete': complete, 'rows': len(bars), 'collected_at': now})
                consecutive_errors = 0
                report['saved'] += 1
                report['rows'] += len(bars)
                if not complete:
                    report['errors'].append({'code': code, 'error': 'Price or fund-flow date coverage incomplete'})
            except Exception as exc:
                consecutive_errors += 1
                report['errors'].append({'code': code, 'error': str(exc)[:160]})
                if 'No historical bars' in str(exc):
                    # Provider confirmed no series for this board; persist the
                    # skip so future runs never re-probe it.
                    skip_list.add(code)
                    skip_added.append(code)
                    await asyncio.to_thread(store.kv_set, f'board_history_skip:{board_type}',
                                            {'codes': sorted(skip_list), 'updated_at': datetime.now(TZ).isoformat(),
                                             'last_added': [code]})
                    consecutive_errors = 0
                elif consecutive_errors >= 12:
                    stop.set()
                else:
                    # Back off after transport errors so throttling windows pass.
                    await asyncio.sleep(10)
            finished = report['saved'] + report['skipped'] + len(report['errors'])
            if finished % 50 == 0:
                print(f'type={board_type} saved={report["saved"]} skipped={report["skipped"]} errors={len(report["errors"])}', flush=True)

    done = 0
    for board in pending:
        if stop.is_set():
            break
        await one(board)
        done += 1
        # Serial pacing: pause briefly every 40 boards so the provider does not
        # throttle the whole run. Checkpoints keep the run resumable.
        if done % 25 == 0:
            await asyncio.sleep(12)
    report['finished_at'] = datetime.now(TZ).isoformat()
    report['status'] = 'partial' if report['errors'] or stop.is_set() else 'finished'
    if skip_added:
        report['skip_added'] = skip_added
        report['skip_list_size'] = len(skip_list)
    await asyncio.to_thread(store.kv_set, report_key, report)
    return report


async def main():
    import json
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--type', type=int, choices=[2, 3], required=True)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    print(json.dumps(await backfill(args.type, args.start, args.end, force=args.force), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
