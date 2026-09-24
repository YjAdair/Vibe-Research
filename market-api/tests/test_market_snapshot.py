import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app.core.store import Store
from app.datasources import eastmoney
from app.services import daily_close, intraday

TZ = ZoneInfo('Asia/Shanghai')
STAMP = int(datetime(2026, 9, 10, 15, 0, tzinfo=TZ).timestamp())


def row(code, price=10, prev=10, amount=100, stamp=STAMP, **extra):
    data = {'f2': price, 'f3': 1.0, 'f6': amount, 'f12': code, 'f13': 1, 'f14': 'N' + code,
            'f15': 11, 'f16': 9, 'f17': 10, 'f18': prev, 'f8': 2.5, 'f10': 1.6, 'f20': 1e10, 'f21': 8e9, 'f100': '行业', 'f103': '概念', 'f124': stamp}
    data.update(extra)
    return data


class MarketSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'db.sqlite'))

    def tearDown(self):
        self.tmp.cleanup()

    def pages(self, hs, bj):
        mapping = {eastmoney.HS_A_FS: hs, eastmoney.BJ_A_FS: bj}
        async def fetch(url, *, params=None, headers=None):
            fs = params['fs']
            pn = int(params['pn'])
            rows = mapping[fs]
            start = (pn - 1) * 100
            return {'data': {'total': len(rows), 'diff': rows[start:start + 100]}}
        return fetch

    async def test_pagination_and_exchange_coverage(self):
        hs = [row(f'{i:06d}') for i in range(101)]
        bj = [row('920001'), row('430002'), row('810014')]
        with patch.object(eastmoney, 'fetch_json', self.pages(hs, bj)):
            snap = await eastmoney.market_snapshot()
        self.assertEqual(snap['hs_count'], 101)
        self.assertEqual(snap['bj_count'], 2)
        self.assertEqual(snap['total'], 103)
        self.assertEqual(snap['skipped_non_a'][0]['code'], '810014')
        self.assertEqual(snap['provider_bj_total'], 3)
        self.assertEqual(snap['trade_date'], '2026-09-10')
        self.assertTrue(snap['complete'])
        self.assertEqual(snap['stocks'][0]['amount'], 100)
        self.assertEqual(snap['stocks'][0]['circulation_value'], 8e9)
        self.assertEqual(snap['stocks'][0]['turnover_ratio'], 2.5)
        self.assertEqual(snap['stocks'][0]['vol_ratio'], 1.6)
        self.assertIsNone(eastmoney._finite(None))

    async def test_rejects_truncated_duplicate_mixed_date_and_missing_stamp(self):
        hs = [row('000001'), row('000002')]
        bj = [row('920001')]
        async def truncated(url, *, params=None, headers=None):
            fs, pn = params['fs'], int(params['pn'])
            if fs == eastmoney.BJ_A_FS:
                return {'data': {'total': 1, 'diff': bj}}
            if pn == 1:
                return {'data': {'total': 2, 'diff': [hs[0]]}}
            return {'data': {'total': 2, 'diff': []}}
        with patch.object(eastmoney, 'fetch_json', truncated), self.assertRaises(ValueError):
            await eastmoney.market_snapshot()
        hs_dup = [row('000001'), row('000001')]
        with patch.object(eastmoney, 'fetch_json', self.pages(hs_dup, bj)), self.assertRaises(ValueError):
            await eastmoney.market_snapshot()
        mixed = [row('000001'), row('000002', stamp=STAMP - 86400)]
        with patch.object(eastmoney, 'fetch_json', self.pages(mixed, bj)), self.assertRaises(ValueError):
            await eastmoney.market_snapshot()
        missing = [row('000001', f124=None), row('000002')]
        with patch.object(eastmoney, 'fetch_json', self.pages(missing, bj)), self.assertRaises(ValueError):
            await eastmoney.market_snapshot()

    async def test_amount_null_is_not_zero_and_metrics_keep_hs_universe(self):
        stocks = [
            {'code': '000001', 'market': 'sz', 'pct': 1, 'amount': 10, 'high': 11, 'price': 10, 'prev_close': 9, 'name': 'A'},
            {'code': '600000', 'market': 'sh', 'pct': -1, 'amount': None, 'high': 10, 'price': 9, 'prev_close': 10, 'name': 'B'},
            {'code': '920001', 'market': 'bj', 'pct': 5, 'amount': 999, 'high': 12, 'price': 11, 'prev_close': 10, 'name': 'C'},
        ]
        metrics = intraday.compute_minute_metrics(stocks[:2])
        self.assertEqual(metrics['amount'], 10)
        self.assertEqual(metrics['amount_missing'], 1)
        self.assertFalse(metrics['amount_complete'])
        self.assertEqual(metrics['up_num'], 1)
        mixed = intraday.compute_minute_metrics(stocks)
        self.assertEqual(mixed['up_num'], 2)

    async def test_incomplete_close_does_not_replace_known_day(self):
        good = [{'stock_code': '000001', 'stock_name': 'A', 'market_type': '深', 'concept': 'x', 'close': 10, 'prev_close': 9}]
        self.db.daily_close_save('2026-09-10', good, {'complete': True, 'trade_date': '2026-09-10'})
        with self.assertRaises(ValueError):
            self.db.daily_close_save('2026-09-10', good, {'complete': False})
        self.assertEqual(self.db.daily_close_range('2026-09-10', 1)[0]['close'], 10)
        hs = [row('000001')]
        bj = [row('920001')]
        with patch.object(eastmoney, 'fetch_json', self.pages(hs, bj)), patch.object(daily_close, 'store', self.db):
            n = await daily_close.backfill_day('2026-09-10')
        self.assertEqual(n, 2)
        self.assertEqual(self.db.daily_close_run('2026-09-10')['bj_count'], 1)
        self.assertEqual(self.db.daily_close_run('2026-09-10')['priced_count'], 2)
        hs_wrong = [row('000001', stamp=STAMP - 86400)]
        with patch.object(eastmoney, 'fetch_json', self.pages(hs_wrong, [row('920001', stamp=STAMP - 86400)])), patch.object(daily_close, 'store', self.db), self.assertRaises(ValueError):
            await daily_close.backfill_day('2026-09-10')
        self.assertEqual(self.db.daily_close_run('2026-09-10')['count'], 2)

    async def test_unpriced_rows_do_not_block_priced_close_publication(self):
        hs = [row('000001'), row('600000', price=None, amount=None, **{'f3': None})]
        bj = [row('920001')]
        with patch.object(eastmoney, 'fetch_json', self.pages(hs, bj)), patch.object(daily_close, 'store', self.db):
            n = await daily_close.backfill_day('2026-09-10')
        self.assertEqual(n, 2)
        meta = self.db.daily_close_run('2026-09-10')
        self.assertEqual(meta['priced_count'], 2)
        self.assertEqual(meta['unpriced_count'], 1)

    async def test_minute_sample_requires_current_complete_snapshot(self):
        hs = [row('000001')]
        bj = [row('920001')]
        frozen = datetime(2026, 9, 10, 15, 0, tzinfo=TZ)
        with patch.object(intraday, '_in_session', return_value=True), patch.object(intraday, 'datetime') as dt, patch.object(intraday, 'store', self.db), patch.object(eastmoney, 'fetch_json', self.pages(hs, bj)), patch.object(eastmoney, 'limit_up_pool', AsyncMock(return_value={'pool': []})), patch.object(eastmoney, 'limit_down_pool', AsyncMock(return_value={'pool': []})):
            dt.now.return_value = frozen
            sample = await intraday.sample_minute()
        self.assertEqual(sample['hs_count'], 1)
        self.assertEqual(sample['bj_count'], 1)
        self.assertEqual(sample['metric_universe'], 'hsj_a_exst')
        self.assertTrue(sample['complete'])
        hs_old = [row('000001', stamp=STAMP - 86400)]
        with patch.object(intraday, '_in_session', return_value=True), patch.object(intraday, 'datetime') as dt, patch.object(intraday, 'store', self.db), patch.object(eastmoney, 'fetch_json', self.pages(hs_old, [row('920001', stamp=STAMP - 86400)])), patch.object(eastmoney, 'limit_up_pool', AsyncMock(return_value={'pool': []})), patch.object(eastmoney, 'limit_down_pool', AsyncMock(return_value={'pool': []})), self.assertRaises(ValueError):
            dt.now.return_value = frozen
            await intraday.sample_minute()

class ShapeCountTests(unittest.TestCase):
    def test_bigleg_mian_and_tiandi_use_published_ohlc(self):
        from app.services.intraday import _shape_counts
        rows = [
            {'stock_code': '000001', 'stock_name': 'A', 'open': 11.0, 'high': 11.0, 'low': 10.2, 'close': 11.0, 'prev_close': 10.0},
            {'stock_code': '000002', 'stock_name': 'B', 'open': 10.2, 'high': 11.0, 'low': 10.1, 'close': 10.3, 'prev_close': 10.0},
            {'stock_code': '000003', 'stock_name': 'C', 'open': 10.5, 'high': 11.0, 'low': 9.0, 'close': 9.0, 'prev_close': 10.0},
        ]
        up = [{'c': '000001'}]
        out = _shape_counts(rows, up, set())
        self.assertEqual(out['bigleg_num'], 1)
        self.assertEqual(out['mian_num'], 1)
        self.assertEqual(out['tiandi_num'], 1)
        self.assertEqual(out['ditian_num'], 0)
        self.assertEqual(_shape_counts([], [], set())['bigleg_num'], None)
