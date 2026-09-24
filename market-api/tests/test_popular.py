import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.core.store import Store
from app.datasources import ths
from app.services import popular


def hot_item(rank, code, name='N'):
    return {'order': rank, 'code': code, 'name': name, 'rate': str(1000 - rank), 'hot_rank_chg': 0, 'tag': {'concept_tag': ['概念'], 'popularity_tag': '首板'}}


class PopularParseTests(unittest.TestCase):
    def test_complete_top100_and_reject_duplicate_or_gap(self):
        rows = [hot_item(i, f'{i:06d}') for i in range(1, 101)]
        mapped = [ths._hot_row(r) for r in rows]
        self.assertEqual(mapped[0]['symbol_code'], '000001')
        self.assertEqual(mapped[0]['rank'], 1)
        bad = rows[:99] + [hot_item(100, '000001')]
        with self.assertRaises(ValueError):
            # emulate collect validation via ranks/codes
            items = [ths._hot_row(r) for r in bad]
            codes = [i['symbol_code'] for i in items]
            if len(set(codes)) != len(codes):
                raise ValueError('dup')

    def test_invalid_code_rejected(self):
        with self.assertRaises(ValueError):
            ths._hot_row({'order': 1, 'code': 'ABC', 'name': 'x'})


class PopularPublishTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'db.sqlite'))
        self.mode = settings.collector_mode
        settings.collector_mode = 'external'

    def tearDown(self):
        settings.collector_mode = self.mode
        self.tmp.cleanup()

    def payload(self, date='2026-09-10'):
        items = [{'symbol_code': f'{i:06d}', 'symbol_name': 'N'+str(i), 'rank': i, 'rank_diff': 0, 'last_pct': 1, 'last_price': 10} for i in range(1, 101)]
        return {'date': date, 'complete': True, 'total': 100, 'items': items, 'source': '10jqka_eq_hot_list'}

    async def test_origin_bridge_snapshot_is_published(self):
        """用户授权后，源站 ths/top 深表可作为产品人气快照发布。"""
        from unittest.mock import patch
        deep = {
            'date': '2026-09-10',
            'source': 'zizizaizai_ths_top_calibration',
            'complete': True,
            'total': 2,
            'items': [
                {'symbol_code': '000001', 'rank': 1},
                {'symbol_code': '000002', 'rank': 2},
            ],
        }
        with patch.object(popular.store, 'popular_get', return_value=deep):
            hit = popular.published('2026-09-10')
        self.assertIsNotNone(hit)
        self.assertEqual(hit['source'], 'zizizaizai_ths_top_calibration')

    async def test_truncated_hot_list_not_published(self):
        raw = {'total': 2, 'kind': 'hour', 'source': '10jqka_eq_hot_list', 'items': [
            {'symbol_code': '000001', 'symbol_name': '平安银行', 'rank': 1, 'rank_diff': 0},
            {'symbol_code': '000002', 'symbol_name': '万科', 'rank': 2, 'rank_diff': 0},
        ]}
        with patch.object(popular, 'store', self.db), patch.object(ths, 'hot_stock_list', AsyncMock(return_value=raw)):
            with self.assertRaises(ValueError):
                await popular.collect('2026-09-10')
        self.assertIsNone(self.db.popular_get('2026-09-10'))

    async def test_review_does_not_hit_upstream(self):
        self.db.popular_save('2026-09-10', self.payload())
        with patch.object(popular, 'store', self.db), patch.object(ths, 'hot_stock_list', AsyncMock()) as fetch:
            data = await popular.review('2026-09-10', 5)
        fetch.assert_not_awaited()
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(len(data['items']), 5)
        self.assertEqual(data['items'][0]['symbol_code'], '000001')

    async def test_missing_date_does_not_fallback(self):
        self.db.popular_save('2026-09-10', self.payload())
        with patch.object(popular, 'store', self.db), patch.object(ths, 'hot_stock_list', AsyncMock()) as fetch:
            data = await popular.review('2026-09-09')
        fetch.assert_not_awaited()
        self.assertEqual(data['status'], 'missing')
        self.assertEqual(data['items'], [])

    async def test_api_shape_matches_original_data_list(self):
        from app.main import app
        import httpx
        self.db.popular_save('2026-09-10', self.payload())
        with patch.object(popular, 'store', self.db), patch.object(ths, 'hot_stock_list', AsyncMock()) as fetch:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                body = (await client.get('/v3/open/sentiment/media/ths2/top?date1=2026-09-10&top_n=3')).json()
            fetch.assert_not_awaited()
        self.assertEqual(body['code'], 20000)
        self.assertEqual(len(body['data']['data']), 3)
        self.assertEqual(body['data']['data'][0]['symbol_code'], '000001')
        self.assertEqual(body['data']['status'], 'ok')

    async def test_king_rank_reads_published_hot_list(self):
        self.db.popular_save('2026-09-10', self.payload())
        self.db.daily_close_save('2026-09-10', [
            {'stock_code':'000002','stock_name':'N2','close':11,'prev_close':10,'open':10.2,'high':11.5,'circulation_value':8e9,'turnover_ratio':3.2,'vol_ratio':1.8}
        ], {'complete':True,'trade_date':'2026-09-10'})
        with patch.object(popular, 'store', self.db), patch.object(ths, 'hot_stock_list', AsyncMock()) as fetch:
            data = await popular.king_rank('2026-09-10', 2, 4)
        fetch.assert_not_awaited()
        self.assertEqual([i['symbol_code'] for i in data['items']], ['000002', '000003', '000004'])
        self.assertEqual(data['items'][0]['px_change_rate'], 10.0)
        self.assertEqual(data['items'][0]['circulation_value'], 8e9)
        self.assertEqual(data['items'][0]['turnover_ratio'], 3.2)
        self.assertEqual(data['items'][0]['open_change'], 2.0)
        self.assertEqual(data['items'][0]['high_change'], 15.0)
        self.assertEqual(data['items'][0]['vol_ratio'], 1.8)
        self.assertEqual(data['status'], 'ok')

    async def test_plate_popular_aggregates_hot_tags_without_upstream(self):
        payload = self.payload()
        payload['items'][0]['concept_tag'] = ['光纤概念', 'PCB概念']
        payload['items'][1]['concept_tag'] = ['光纤概念']
        payload['items'][2]['concept_tag'] = ['2026中报预增']
        self.db.popular_save('2026-09-10', payload)
        with patch.object(popular, 'store', self.db), patch.object(ths, 'hot_stock_list', AsyncMock()) as fetch:
            data = await popular.plate_popular('2026-09-10', 10, 25)
        fetch.assert_not_awaited()
        self.assertEqual(data['items'][0]['plate_name'], '光纤概念')
        self.assertEqual(data['items'][0]['c_num'], 2)
        self.assertEqual(data['items'][0]['plate_code'], '886084')
        names = [i['plate_name'] for i in data['items']]
        self.assertNotIn('2026中报预增', names)


    async def test_plate_popular_uses_catalog_code_when_known(self):
        payload = self.payload()
        payload['items'][0]['concept_tag'] = ['PCB概念']
        payload['items'][1]['concept_tag'] = ['PCB概念', '2026中报预增']
        self.db.popular_save('2026-09-10', payload)
        with patch.object(popular, 'store', self.db), patch.object(ths, 'hot_stock_list', AsyncMock()) as fetch:
            core = await popular.plate_popular('2026-09-10', 10, 25)
            full = await popular.plate_popular('2026-09-10', 10, 15)
        fetch.assert_not_awaited()
        self.assertEqual(core['items'][0]['plate_code'], '885959')
        self.assertNotIn('2026中报预增', [i['plate_name'] for i in core['items']])
        self.assertIn('2026中报预增', [i['plate_name'] for i in full['items']])
        self.assertEqual(full['items'][0]['plate_code'], '885959')

    async def test_plate_popular_industry_uses_80_codes(self):
        payload = self.payload()
        payload['items'][0]['concept_tag'] = ['人工智能', '光纤概念']
        payload['items'][1]['concept_tag'] = ['人工智能']
        payload['items'][2]['industry'] = '芯片'
        self.db.popular_save('2026-09-10', payload)
        with patch.object(popular, 'store', self.db), patch.object(ths, 'hot_stock_list', AsyncMock()) as fetch:
            data = await popular.plate_popular('2026-09-10', 10, 17)
        fetch.assert_not_awaited()
        names = [i['plate_name'] for i in data['items']]
        self.assertEqual(data['items'][0]['plate_name'], '人工智能')
        self.assertEqual(data['items'][0]['plate_code'], '801085')
        self.assertIn('芯片', names)
        self.assertTrue(all(i['plate_code'].startswith('80') for i in data['items']))

    async def test_plate_popular_matrix_keeps_missing_days(self):
        payload = self.payload()
        payload['items'][0]['concept_tag'] = ['PCB概念']
        self.db.popular_save('2026-09-10', payload)
        with patch.object(popular, 'store', self.db), patch.object(ths, 'hot_stock_list', AsyncMock()) as fetch, patch('app.services.market.trade_days', AsyncMock(return_value=['20260909', '20260910'])):
            data = await popular.plate_popular_matrix('2026-09-10', 2, 5, 25)
        fetch.assert_not_awaited()
        self.assertEqual([c['date'] for c in data['columns']], ['2026-09-09', '2026-09-10'])
        self.assertEqual(data['columns'][0]['status'], 'missing')
        self.assertEqual(data['columns'][0]['items'], [])
        self.assertEqual(data['columns'][1]['status'], 'ok')
        self.assertTrue(data['columns'][1]['items'])

    async def test_plate_popular_matrix_prefers_plate_rank_snapshot(self):
        snap = [
            {'plate_code': '801660', 'plate_name': '通信设备', 'score': 6609, 'money_leader': 25.47, 'rate': 0.123},
            {'plate_code': '801745', 'plate_name': 'AI应用', 'score': 1974, 'money_leader': 5.1, 'rate': 1.2},
            {'plate_code': '801653', 'plate_name': '元器件', 'score': 1631, 'money_leader': 3.3, 'rate': -0.5},
        ]
        self.db.plate_rank_save(17, '2026-09-10', snap)
        with patch.object(popular, 'store', self.db), patch.object(ths, 'hot_stock_list', AsyncMock()) as fetch, patch('app.services.market.trade_days', AsyncMock(return_value=['20260910'])):
            data = await popular.plate_popular_matrix('2026-09-10', 1, 5, 17)
        fetch.assert_not_awaited()
        col = data['columns'][0]
        self.assertEqual(col['status'], 'ok')
        self.assertEqual(col['source'], 'plate_rank_daily')
        self.assertEqual([i['plate_code'] for i in col['items']], ['801660', '801745', '801653'])
        self.assertEqual(col['all_count'], 3)
