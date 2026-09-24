"""补数不能把最新日期当作历史覆盖证明。"""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
from app.services import ths_board_backfill as service
BAR = {'date': '2026-09-11', 'open': 10, 'high': 12, 'low': 9, 'close': 11, 'volume': 1, 'amount': 10}
async def run_case(years=None, latest='2026-09-11', now=None, bars=None):
    db = MagicMock()
    db.ths_board_daily_range.return_value = []
    db.kv_get.return_value = {'days': ['20260911', '20260914', '20261231']}
    db.ths_board_daily_stats.return_value = [{'board_code': '885001', 'latest': latest}]
    fetch = AsyncMock(return_value={'bars': bars if bars is not None else [BAR]})
    with patch.object(service, 'store', db), patch.object(service, 'load_catalog', return_value=[{'code': '885001', 'kind': 'concept'}]), patch.object(service, 'fetch_board_bars', fetch), patch.object(service, 'datetime', wraps=datetime) as clock:
        clock.now.return_value = now or datetime(2026, 9, 14, 11, tzinfo=service.TZ)
        report = await service.backfill(years=years)
    return report, fetch, db
def test_future_calendar_day_not_used_and_previous_close_skipped():
    report, fetch, _ = asyncio.run(run_case())
    assert report['expected_completed_day'] == '2026-09-11'
    assert report['skipped_fresh'] == 1
    fetch.assert_not_awaited()
def test_explicit_history_years_not_skipped_for_recent_latest_bar():
    report, fetch, _ = asyncio.run(run_case(years=[2023]))
    assert report['saved'] == 1
    assert fetch.await_args.kwargs['years'] == [2023]
def test_today_partial_bar_refreshed_after_close():
    report, fetch, _ = asyncio.run(run_case(latest='2026-09-14', now=datetime(2026, 9, 14, 16, tzinfo=service.TZ)))
    assert report['expected_completed_day'] == '2026-09-14'
    fetch.assert_awaited_once()
def test_future_bars_never_written():
    report, _, db = asyncio.run(run_case(years=[2023], bars=[BAR, {**BAR, 'date': '2026-12-31'}]))
    assert report['saved'] == 1
    assert db.ths_board_daily_save.call_args.args[1] == [BAR]

def test_recent_success_does_not_claim_requested_year_coverage():
    report, _, _ = asyncio.run(run_case(years=[2023]))
    assert report['status'] == 'finished_with_gaps'
    assert report['coverage_gaps'] == [{'code': '885001', 'year': 2023, 'reason': 'requested_year_missing'}]
