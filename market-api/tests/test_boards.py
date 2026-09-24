import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.core.store import Store
from app.datasources import eastmoney
from app.services import boards
from app.core.cache import TTLCache


class BoardDataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'test.db'))
        boards._locks.clear()

    def tearDown(self):
        self.tmp.cleanup()

    async def test_money_fields_not_interchangeable(self):
        raw = {'data': {'total': 1, 'diff': [{'f12': 'BK0001', 'f6': 800,
                'f62': -90, 'f66': 25, 'f124': 1789020000}]}}
        with patch.object(eastmoney, 'fetch_json', AsyncMock(return_value=raw)):
            for response, key in [(await eastmoney.board_list(), 'boards'),
                                  (await eastmoney.board_stocks('BK0001'), 'stocks')]:
                row = response[key][0]
                self.assertEqual(row['amount'], 800)
                self.assertEqual(row['main_net_inflow'], -90)

    async def test_pagination_and_duplicate_rejection(self):
        pages = {1: {'total': 3, 'boards': [{'plate_code': 'A'}, {'plate_code': 'B'}]},
                 2: {'total': 3, 'boards': [{'plate_code': 'C'}]}}
        fetch = AsyncMock(side_effect=lambda p: pages[p])
        self.assertEqual(len(await boards.all_pages(fetch, 'boards')), 3)
        self.assertEqual(fetch.await_count, 2)
        pages[2]['boards'] = [{'plate_code': 'B'}]
        with self.assertRaises(ValueError):
            await boards.all_pages(fetch, 'boards')

    async def test_historical_date_never_fetches_current_data(self):
        self.db.board_snapshot_save(2, '2026-09-10', [{'plate_code': 'A', 'date1': '2026-09-10', 'pct': 5}], {'trade_date': '2026-09-10'})
        with patch.object(boards, 'store', self.db), patch.object(boards, 'collect', AsyncMock()) as collect, patch('app.services.market.trade_days', AsyncMock(return_value=['20260909', '20260910'])):
            result = await boards.evolution(2, '20260909', 1, 20, 'pct')
            self.assertEqual(result['columns'][0]['status'], 'missing')
            self.assertEqual(result['columns'][0]['items'], [])
            collect.assert_not_awaited()

    async def test_collect_single_flight_and_failed_batch_preserves_snapshot(self):
        row = {'plate_code': 'BK0001', 'source_timestamp': 1789020000, 'pct': 1,
               'amount': 100, 'main_net_inflow': -2}
        fetch = AsyncMock(return_value=[row])
        with patch.object(boards, 'store', self.db), patch.object(boards, 'cache', TTLCache()), patch.object(boards, 'all_pages', fetch):
            results = await asyncio.gather(*(boards.collect(2) for _ in range(8)))
            self.assertEqual(fetch.await_count, 1)
            day = results[0]['trade_date']
            boards.cache.delete('board_snapshot_v1:2')
            fetch.side_effect = ValueError('Incomplete upstream batch')
            with self.assertRaises(ValueError):
                await boards.collect(2)
            rows, _ = self.db.board_snapshot_range(2, day, day)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['amount'], 100)

    async def test_route_validation_and_historical_contract(self):
        from app.main import app
        transport = httpx.ASGITransport(app=app)
        with patch.object(boards, 'store', self.db), patch('app.services.market.trade_days', AsyncMock(return_value=['20260909'])):
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                for query in ['days=0', 'sort_by=bogus', 'date1=2026-02-30']:
                    response = await client.get('/v3/market/plates/2/evolution?' + query)
                    self.assertEqual(response.status_code, 422)
                response = await client.get('/v3/market/plates/2/rank?date1=20260909')
                self.assertEqual(response.json()['data'], [])


if __name__ == '__main__':
    unittest.main()
