import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.core.store import Store
from app.datasources import cninfo, tencent
from app.services import index_history, movement


def bar(day, close, prev, name='金健米业'):
    return {'trade_date': day, 'stock_code': '600127', 'stock_name': name, 'close': close, 'prev_close': prev}


def idx(day, close):
    return {'trade_date': day, 'open': close, 'close': close, 'high': close, 'low': close}


class MovementFormulaTests(unittest.TestCase):
    def test_best_window_matches_known_start_and_index_deviation(self):
        closes = [
            bar('2026-08-28', 11.77, 10.78),
            bar('2026-08-31', 12.04, 11.77),
            bar('2026-09-01', 13.24, 12.04),
            bar('2026-09-02', 12.40, 13.24),
            bar('2026-09-03', 12.67, 12.40),
            bar('2026-09-04', 12.30, 12.67),
            bar('2026-09-07', 13.53, 12.30),
            bar('2026-09-08', 14.79, 13.53),
            bar('2026-09-09', 14.33, 14.79),
            bar('2026-09-10', 14.00, 14.33),
        ]
        indexes = [
            idx('2026-08-27', 4148.40),
            idx('2026-08-28', 4143.84),
            idx('2026-08-31', 4179.61),
            idx('2026-09-01', 4173.03),
            idx('2026-09-02', 4132.65),
            idx('2026-09-03', 4133.31),
            idx('2026-09-04', 4120.71),
            idx('2026-09-07', 4123.41),
            idx('2026-09-08', 4131.67),
            idx('2026-09-09', 4143.22),
            idx('2026-09-10', 4125.36),
        ]
        t1 = movement._best_window(closes, indexes, '2026-09-10', 10, 100)
        self.assertEqual(t1['start_date'], '2026-08-31')
        self.assertEqual(t1['start_pre_close'], 11.77)
        self.assertEqual(t1['index_start_pre_close'], 4143.84)
        self.assertEqual(t1['index_last_px'], 4125.36)
        self.assertEqual(t1['gain_pct'], 18.95)
        self.assertEqual(t1['index_gain_pct'], -0.45)
        self.assertEqual(t1['space_pct'], 80.6)
        self.assertAlmostEqual(t1['threshold_price'], 23.44, places=2)
        self.assertEqual(t1['days'], 9)

    def test_notice_window_marks_exchange_monitor(self):
        days = [f'2026-09-{i:02d}' for i in range(1, 11)]
        closes = [bar(d, 10 + i, 10 + i - 0.1) for i, d in enumerate(days, 1)]
        extra = ['2026-08-31'] + days
        indexes = {'series': {'000002': {'bars': [idx(d, 4000) for d in extra]}}}
        notices = [{'stock_code': '600127', 'notice_date': '2026-09-02', 'title': '严重异常波动'}]
        rows = movement.compute('2026-09-10', closes, indexes, notices)
        self.assertEqual(rows[0]['is_monitored'], True)
        self.assertEqual(rows[0]['monitor_start_date'], '2026-09-02')

    def test_cninfo_parser_keeps_severe_notices_only(self):
        total, items = cninfo.parse_page({
            'totalAnnouncement': 2,
            'announcements': [
                {'secCode': '000017', 'secName': '深中华A', 'announcementTitle': '股票交易<em>严重</em>异常波动公告', 'announcementTime': 1788364800000},
                {'secCode': '000001', 'secName': '平安银行', 'announcementTitle': '异常波动公告', 'announcementTime': 1788364800000},
            ],
        })
        self.assertEqual(total, 2)
        self.assertEqual([i['stock_code'] for i in items], ['000017'])
        self.assertEqual(items[0]['notice_date'], '2026-09-03')


class MovementPublishTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'db.sqlite'))
        self.mode = settings.collector_mode
        settings.collector_mode = 'external'

    def tearDown(self):
        settings.collector_mode = self.mode
        self.tmp.cleanup()

    async def test_incomplete_index_history_is_not_published(self):
        with patch.object(index_history, 'store', self.db), patch.object(tencent, 'kline_day_by_symbol', AsyncMock(return_value={'x': [], 'y': []})):
            with self.assertRaises(ValueError):
                await index_history.collect('2026-09-10')
        self.assertIsNone(self.db.index_history_get('2026-09-10'))

    async def test_review_reads_snapshot_without_upstream(self):
        payload = {'date': '2026-09-10', 'complete': True, 'total': 1, 'items': [{'symbol_code': '600127', 'symbol_name': '金健米业', 't1_space_pct': 4.3}], 'source': 'test'}
        self.db.movement_save('2026-09-10', payload)
        with patch.object(movement, 'store', self.db), patch.object(cninfo, 'severe_notices', AsyncMock()) as notices, patch.object(index_history, 'collect', AsyncMock()) as hist:
            data = await movement.review('2026-09-10')
        notices.assert_not_awaited(); hist.assert_not_awaited()
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(data['items'][0]['symbol_code'], '600127')

    async def test_missing_date_does_not_fallback(self):
        self.db.movement_save('2026-09-10', {'date': '2026-09-10', 'complete': True, 'total': 0, 'items': []})
        with patch.object(movement, 'store', self.db):
            data = await movement.review('2026-09-09')
        self.assertEqual(data['status'], 'missing')
        self.assertEqual(data['items'], [])

    async def test_api_does_not_hit_upstream(self):
        from app.main import app
        import httpx
        self.db.movement_save('2026-09-10', {'date': '2026-09-10', 'complete': True, 'total': 1, 'items': [{'symbol_code': '600127'}]})
        with patch.object(movement, 'store', self.db), patch.object(cninfo, 'severe_notices', AsyncMock()) as notices:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                v3 = (await client.get('/v3/market/movement/alerts?date1=2026-09-10')).json()
                legacy = (await client.get('/market/movement/alerts?date1=2026-09-10')).json()
            notices.assert_not_awaited()
        self.assertEqual(v3['code'], 20000)
        self.assertEqual(legacy['data']['items'][0]['symbol_code'], '600127')

    def test_ensure_close_ready_reconstructs_from_rows(self):
        day = '2026-09-10'
        rows = [
            {'stock_code': f'{i:06d}', 'close': 10.0, 'prev_close': 9.0, 'stock_name': 'x'}
            for i in range(1000)
        ]
        self.db.daily_close_upsert([{**r, 'trade_date': day} for r in rows])
        with patch.object(movement, 'store', self.db):
            meta = movement.ensure_close_ready(day)
        self.assertTrue(meta['complete'])
        self.assertEqual(meta['priced_count'], 1000)
        self.assertEqual(meta['source'], 'reconstructed_from_daily_close_rows')
        self.assertEqual(self.db.daily_close_run(day)['priced_count'], 1000)
