import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.core.store import Store
from app.datasources.board_history import parse_series
from app.services import boards, board_backfill


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'test.db'))

    def tearDown(self):
        self.tmp.cleanup()

    def test_independent_schemas_and_nulls(self):
        bar = '2026-09-09,10,11,12,9,100,1000,30,10,1,2'
        flow = '2026-09-09,-25,10,5,-20,-5,-2.5,1,.5,-2,-.5,11,10,0,0'
        def response(value):
            return {'rc': 0, 'data': {'code': 'BK0001', 'klines': [value]}}
        row = parse_series(response(bar), 'BK0001', '2026-09-01', '2026-09-10')[0]
        self.assertEqual((row['amount'], row['pct'], row['price']), (1000, 10, 11))
        row = parse_series(response(flow), 'BK0001', '2026-09-01', '2026-09-10', True)[0]
        self.assertEqual(row['main_net_inflow'], -25)
        for payload in [response(bar + ',0'), response(bar.replace(',1000,', ',nan,')), response(bar.replace(',12,9,', ',8,9,'))]:
            with self.assertRaises(ValueError):
                parse_series(payload, 'BK0001', '2026-09-01', '2026-09-10')

    async def test_snapshot_precedence_and_missing_metric_not_ranked(self):
        self.db.board_history_save(3, 'BK0001', [{'date1': '2026-09-09', 'plate_code': 'BK0001', 'pct': 3, 'main_net_inflow': -50, 'up_count': None}])
        self.db.board_history_save(3, 'BK0002', [{'date1': '2026-09-09', 'plate_code': 'BK0002', 'pct': 4, 'main_net_inflow': None}])
        self.db.board_snapshot_save(3, '2026-09-09', [{'date1': '2026-09-09', 'plate_code': 'BK0001', 'pct': 5, 'main_net_inflow': -40}], {'trade_date': '2026-09-09'})
        with patch.object(boards, 'store', self.db), patch('app.services.market.trade_days', AsyncMock(return_value=['20260909'])):
            result = await boards.evolution(3, '20260909', 1, 10, 'flow')
            col = result['columns'][0]
            self.assertEqual(col['ranked_count'], 1)
            self.assertEqual(col['missing_metric_count'], 1)
            self.assertEqual(col['items'][0]['pct'], 5)
            self.assertEqual(col['items'][0]['main_net_inflow'], -40)

    def test_partial_retry_preserves_known_flow(self):
        row = {'date1': '2026-09-09', 'plate_code': 'BK0001', 'main_net_inflow': -25}
        self.db.board_history_save(3, 'BK0001', [row])
        self.db.board_history_save(3, 'BK0001', [{**row, 'main_net_inflow': None}])
        self.assertEqual(self.db.board_history_range(3, '2026-09-09', '2026-09-09')[0]['main_net_inflow'], -25)

    async def test_outage_stops_before_fanout(self):
        universe = [{'plate_code': 'BK' + str(i).zfill(4)} for i in range(100)]
        self.db.board_snapshot_save(3, '2026-09-10', universe, {'trade_date': '2026-09-10'})
        with patch.object(board_backfill, 'store', self.db), patch.object(board_backfill, 'collect', AsyncMock(return_value={'trade_date': '2026-09-10'})), patch.object(board_backfill, 'fetch_series', AsyncMock(side_effect=ValueError('upstream down'))) as fetch:
            report = await board_backfill.backfill(3, '20260901', '20260909')
        self.assertEqual(report['status'], 'source_unavailable')
        self.assertEqual(fetch.await_count, 1)
        self.assertEqual(self.db.board_history_range(3, '2026-09-01', '2026-09-09'), [])

    async def test_checkpoint_resume_and_history_contract(self):
        self.db.board_snapshot_save(3, '2026-09-10', [{'plate_code': 'BK0001', 'plate_name': '测试板块'}], {'trade_date': '2026-09-10'})
        async def source(code, start, end, flow=False):
            if flow:
                return [{'date1': '2026-09-09', 'main_net_inflow': -55}]
            return [{'date1': '2026-09-09', 'pct': 2, 'amount': 100, 'price': 10}]
        with patch.object(board_backfill, 'store', self.db), patch.object(board_backfill, 'collect', AsyncMock(return_value={'trade_date': '2026-09-10'})), patch.object(board_backfill, 'fetch_series', AsyncMock(side_effect=source)) as fetch, patch('app.services.market.trade_days', AsyncMock(return_value=['20260909'])):
            first = await board_backfill.backfill(3, '20260909', '20260909')
            self.assertEqual(first['status'], 'finished')
            self.assertEqual(first['rows'], 1)
            fetch.reset_mock()
            second = await board_backfill.backfill(3, '20260909', '20260909')
            self.assertEqual(second['skipped'], 1)
            fetch.assert_not_awaited()
        with patch.object(boards, 'store', self.db), patch('app.services.market.trade_days', AsyncMock(return_value=['20260909'])):
            data = await boards.evolution(3, '20260909', 1, 20, 'flow')
        self.assertEqual(data['columns'][0]['items'][0]['main_net_inflow'], -55)
        self.assertEqual(data['columns'][0]['data_kind'], 'historical_daily')

    async def test_skip_list_short_circuits_provider_confirmed_boards(self):
        universe = [{'plate_code': 'BK0001', 'plate_name': 'A'}, {'plate_code': 'BK0002', 'plate_name': 'B'}]
        self.db.board_snapshot_save(3, '2026-09-10', universe, {'trade_date': '2026-09-10'})
        self.db.kv_set('board_history_skip:3', {'codes': ['BK0001']})
        calls = []
        async def source(code, start, end, flow=False):
            calls.append(code)
            if flow:
                return [{'date1': '2026-09-09', 'main_net_inflow': -10}]
            return [{'date1': '2026-09-09', 'pct': 1, 'amount': 100, 'price': 10}]
        with patch.object(board_backfill, 'store', self.db), patch.object(board_backfill, 'collect', AsyncMock(return_value={'trade_date': '2026-09-10'})), patch.object(board_backfill, 'fetch_series', AsyncMock(side_effect=source)), patch('app.services.market.trade_days', AsyncMock(return_value=['20260909'])):
            report = await board_backfill.backfill(3, '20260909', '20260909')
        self.assertEqual(report['status'], 'finished')
        self.assertEqual(report['skipped'], 1)
        self.assertEqual(report['saved'], 1)
        self.assertEqual(report['skip_list_size'], 1)
        self.assertEqual(sorted(set(calls)), ['BK0002'])

    async def test_no_data_board_enters_skip_list_and_persists(self):
        universe = [{'plate_code': 'BK0001', 'plate_name': 'A'}, {'plate_code': 'BK0002', 'plate_name': 'B'}]
        self.db.board_snapshot_save(3, '2026-09-10', universe, {'trade_date': '2026-09-10'})
        async def source(code, start, end, flow=False):
            if code == 'BK0001' and not flow:
                raise ValueError('No historical bars for board BK0001')
            if flow:
                return [{'date1': '2026-09-09', 'main_net_inflow': -10}]
            return [{'date1': '2026-09-09', 'pct': 1, 'amount': 100, 'price': 10}]
        with patch.object(board_backfill, 'store', self.db), patch.object(board_backfill, 'collect', AsyncMock(return_value={'trade_date': '2026-09-10'})), patch.object(board_backfill, 'fetch_series', AsyncMock(side_effect=source)), patch('app.services.market.trade_days', AsyncMock(return_value=['20260909'])):
            report = await board_backfill.backfill(3, '20260909', '20260909')
        self.assertIn('BK0001', report['skip_added'])
        persisted = self.db.kv_get('board_history_skip:3')
        self.assertIn('BK0001', persisted['codes'])
        self.assertNotIn('BK0002', persisted['codes'])

    async def test_probe_skips_no_data_board_and_continues(self):
        universe = [{'plate_code': 'BK0001', 'plate_name': 'A'}, {'plate_code': 'BK0002', 'plate_name': 'B'}]
        self.db.board_snapshot_save(3, '2026-09-10', universe, {'trade_date': '2026-09-10'})
        async def source(code, start, end, flow=False):
            if code == 'BK0001':
                raise ValueError('No historical bars for board BK0001')
            if flow:
                return [{'date1': '2026-09-09', 'main_net_inflow': -10}]
            return [{'date1': '2026-09-09', 'pct': 1, 'amount': 100, 'price': 10}]
        with patch.object(board_backfill, 'store', self.db), patch.object(board_backfill, 'collect', AsyncMock(return_value={'trade_date': '2026-09-10'})), patch.object(board_backfill, 'fetch_series', AsyncMock(side_effect=source)), patch('app.services.market.trade_days', AsyncMock(return_value=['20260909'])):
            report = await board_backfill.backfill(3, '20260909', '20260909')
        self.assertEqual(report['status'], 'finished')
        self.assertIn('BK0001', self.db.kv_get('board_history_skip:3')['codes'])


if __name__ == '__main__':
    unittest.main()
