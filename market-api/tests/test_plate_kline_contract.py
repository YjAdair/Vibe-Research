import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.core.cache import TTLCache
from app.core.store import Store
from app.datasources import tencent, zizizaizai
from app.services import market


class PlateKlineContractTests(unittest.IsolatedAsyncioTestCase):
    """原站 StockKlineDay splitData 契约：{x, y:[o,c,h,l,preclose], vol, turnover}。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'db.sqlite'))
        self.origin = settings.enable_origin_reference
        settings.enable_origin_reference = True  # explicit offline calibration fixtures
        self.mode = settings.collector_mode
        settings.collector_mode = 'external'

    def tearDown(self):
        settings.collector_mode = self.mode
        settings.enable_origin_reference = self.origin
        self.tmp.cleanup()

    async def test_tencent_kline_y4_is_prev_close(self):
        payload = {'data': {'sh600519': {'day': [
            ['2026-09-09', '1400.0', '1410.0', '1420.0', '1395.0', '30000'],
            ['2026-09-10', '1412.0', '1430.0', '1435.0', '1405.0', '32000'],
        ]}}}
        with patch.object(tencent, 'fetch_json', AsyncMock(return_value=payload)):
            data = await tencent.kline_day_by_symbol('sh600519', 2)
        self.assertIsNone(data['y'][0][4])
        self.assertEqual(data['y'][1][4], 1410.0)
        self.assertEqual(data['y'][1][:4], [1412.0, 1430.0, 1435.0, 1405.0])

    async def test_sina_kline_y4_is_prev_close(self):
        rows = [
            {'day': '2026-09-09', 'open': '1400', 'high': '1420', 'low': '1395', 'close': '1410', 'volume': '30000'},
            {'day': '2026-09-10', 'open': '1412', 'high': '1435', 'low': '1405', 'close': '1430', 'volume': '32000'},
        ]
        from app.datasources import sina
        with patch.object(sina, 'fetch_json', AsyncMock(return_value=rows)):
            data = await sina.kline_day('600519', 2)
        self.assertIsNone(data['y'][0][4])
        self.assertEqual(data['y'][1][4], 1410.0)

    async def test_plate_origin_cache_skips_upstream(self):
        series = {'x': ['20260909', '20260910'],
                  'y': [[1.0, 2.0, 3.0, 0.5, None], [2.0, 2.5, 2.8, 1.9, 2.0]],
                  'vol': [100, 120], 'turnover': [1e8, 1.2e8]}
        self.db.plate_kline_origin_save('main', '801120', series)
        fetch = AsyncMock()
        with patch.object(market, 'store', self.db), patch.object(market, 'cache', TTLCache()), \
                patch.object(zizizaizai, 'plate_kline_main', fetch):
            data = await market.plate_kline_contract('801120', 'main')
        fetch.assert_not_awaited()
        self.assertEqual(data['x'], series['x'])
        self.assertEqual(data['y'], series['y'])
        self.assertEqual(data['turnover'], series['turnover'])

    async def test_sub_falls_back_to_main_series(self):
        series = {'x': ['20260910'], 'y': [[2.0, 2.5, 2.8, 1.9, 2.0]], 'vol': [120], 'turnover': [1.2e8]}
        self.db.plate_kline_origin_save('main', '801003', series)
        with patch.object(market, 'store', self.db), patch.object(market, 'cache', TTLCache()), \
                patch.object(zizizaizai, 'plate_kline_sub',
                             AsyncMock(return_value={'x': [], 'y': [], 'vol': [], 'turnover': None})):
            data = await market.plate_kline_contract('801003', 'sub')
        self.assertEqual(data['x'], ['20260910'])
        self.assertEqual(data['y'][0][4], 2.0)

    async def test_881_bars_conversion_preclose_chain(self):
        bars = [
            {'date': '2026-09-09', 'open': 10.0, 'high': 11.0, 'low': 9.5, 'close': 10.5, 'volume': 5000, 'amount': 5.2e7},
            {'date': '2026-09-10', 'open': 10.6, 'high': 11.2, 'low': 10.4, 'close': 11.0, 'volume': 6000, 'amount': 6.5e7},
        ]
        with patch.object(market, 'store', self.db), patch.object(market, 'cache', TTLCache()), \
                patch.object(self.db, 'ths_board_daily_range', lambda *a, **k: bars):
            data = await market.plate_kline_contract('881101', 'main')
        self.assertEqual(data['x'], ['20260909', '20260910'])
        self.assertIsNone(data['y'][0][4])
        self.assertEqual(data['y'][1], [10.6, 11.0, 11.2, 10.4, 10.5])
        self.assertEqual(data['turnover'], [5.2e7, 6.5e7])
        self.assertEqual(data['amount'], [5.2e7, 6.5e7])
        self.assertEqual(data['turnover_semantics'], 'amount_cny')
        self.assertEqual(data['amount_unit'], 'CNY')
        self.assertEqual(data['as_of_date'], '20260910')
        self.assertEqual(data['frequency'], 'daily')

    async def test_subplate_route_returns_contract_shape(self):
        series = {'x': ['20260910'], 'y': [[2.0, 2.5, 2.8, 1.9, 2.0]], 'vol': [120], 'turnover': [1.2e8]}
        self.db.plate_kline_origin_save('sub', '801003', series)
        from app.main import app
        import httpx
        with patch.object(market, 'store', self.db), patch.object(market, 'cache', TTLCache()), \
                patch.object(zizizaizai, 'plate_kline_sub', AsyncMock()) as fetch:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                body = (await client.get('/v3/market/kline/sub-plate/801003')).json()
            fetch.assert_not_awaited()
        self.assertEqual(body['code'], 20000)
        data = body['data']
        self.assertTrue({'turnover', 'vol', 'x', 'y'} <= data.keys())
        self.assertEqual(data['amount'], [1.2e8])
        self.assertEqual(data['turnover_semantics'], 'amount_cny')
        self.assertEqual(data['as_of_date'], '20260910')
        self.assertEqual(len(data['y'][0]), 5)
        self.assertEqual(data['turnover'], [1.2e8])

    async def test_default_origin_disabled_does_not_serve_legacy_cache(self):
        """关闭校准源后不读 plate_kline_origin；无成分收盘则诚实空序列（不假蜡烛）。"""
        settings.enable_origin_reference = False
        self.db.plate_kline_origin_save('main', '801120', {'x': ['20260910'], 'y': [[1, 2, 3, 1, None]]})
        with patch.object(market, 'store', self.db), patch.object(market, 'cache', TTLCache()), \
                patch.object(zizizaizai, 'plate_kline_main', AsyncMock()) as fetch, \
                patch.object(market, 'plate_ew_daily_nav', return_value={
                    'x': [], 'y': [], 'vol': [], 'turnover': None, 'amount': None,
                    'series_kind': 'daily_nav', 'status': 'missing',
                    'reason': 'missing_member_closes', 'source': 'ew_member_daily_close',
                    'frequency': 'daily', 'as_of_date': None,
                }):
            data = await market.plate_kline_contract('801120')
        self.assertEqual(data['x'], [])
        self.assertEqual(data['series_kind'], 'daily_nav')
        self.assertEqual(data['status'], 'missing')
        fetch.assert_not_awaited()

    async def test_ew_daily_nav_from_member_closes(self):
        self.db.daily_close_save('2026-09-09', [
            {'stock_code': '600000', 'close': 10.0},
            {'stock_code': '600001', 'close': 20.0},
        ], metadata={'complete': True})
        self.db.daily_close_save('2026-09-10', [
            {'stock_code': '600000', 'close': 11.0},
            {'stock_code': '600001', 'close': 22.0},
        ], metadata={'complete': True})
        with patch.object(market, 'store', self.db), \
                patch('app.services.plate_flow.plate_members_full', return_value={'600000', '600001'}):
            data = market.plate_ew_daily_nav('801999', n=10, date_end='2026-09-10')
        self.assertEqual(data['series_kind'], 'daily_nav')
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(data['x'], ['20260910'])
        # 等权 +10% → 1100
        self.assertEqual(data['y'], [1100.0])
        self.assertEqual(data['source'], 'ew_member_daily_close')

    async def test_origin_ohlc_preferred_over_nav(self):
        settings.enable_origin_reference = True
        series = {'x': ['20260910'], 'y': [[2.0, 2.5, 2.8, 1.9, 2.0]], 'vol': [120], 'turnover': [1.2e8]}
        self.db.plate_kline_origin_save('main', '801120', series)
        with patch.object(market, 'store', self.db), patch.object(market, 'cache', TTLCache()), \
                patch.object(zizizaizai, 'plate_kline_main', AsyncMock()), \
                patch.object(market, 'plate_ew_daily_nav') as nav:
            data = await market.plate_kline_contract('801120', 'main')
        nav.assert_not_called()
        self.assertEqual(data['series_kind'], 'ohlc')
        self.assertEqual(data['y'][0][:4], [2.0, 2.5, 2.8, 1.9])

    def test_is_plate_code_segments(self):
        self.assertTrue(market.is_plate_code('801120'))
        self.assertTrue(market.is_plate_code('881101'))
        self.assertTrue(market.is_plate_code('883404'))
        self.assertFalse(market.is_plate_code('600519'))
        self.assertFalse(market.is_plate_code('80112'))


if __name__ == '__main__':
    unittest.main()
