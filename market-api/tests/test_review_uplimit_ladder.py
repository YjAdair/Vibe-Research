"""ml 页涨停梯队（review/uplimit/hot?board=）快照推导测试。

原站 board 解析 = 复盘系统当日跟踪板块集合（含 803xxx），而非成分∩涨停池；
过滤语义已对 2026-09-11 原站 801660 完整响应逐字节对拍、801807 空模板对拍
（规则详见 open_routes._derive_board_from_snapshot docstring）。
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.api.open_routes import _derive_board_from_snapshot, review_uplimit_hot
from app.core.store import Store

KEY_ORDER = ['today', 'plate', 'plate_info', 'plate_stocks', 'plate_stocks_zb',
             'plate_stocks_bx', 'stocks', 'max_count', 'relay',
             'plate_stocks_next_yi', 'stock_info', 'stocks_hot', 'stocks_hot_n',
             'ban_info']


def _row(code, name, kt, desc, typ='自', tm='10:00', plate='801660'):
    return {
        'stock_code': code, 'stock_name': name, 'up_limit_type': typ,
        'up_limit_time': tm, 'up_limit_desc': desc, 'up_limit_keep_times': kt,
        'auction_turnover': 0.0, 'checked': None, 'plate_code': plate,
        'market_c': 10.0, 'market_c_c': 8.0, 'fd_max': 1.0, 'fd_close': 0.5,
        'amount': 2.0, 'next_day': None, 'next_open_pct': None, 'next_close_pct': None,
    }


SNAP = {
    'today': False,
    'plate': [['通信', '801660', 6609], ['AI应用', '803023', 1974]],
    'plate_info': {'801660': {'score': 6609, 'name': '通信', 'code': '801660'},
                   '803023': {'score': 1974, 'name': 'AI应用', 'code': '803023'}},
    'plate_stocks': {
        '801660': [
            _row('603421', '鼎信通讯', 3, '3连板', '烂3', '10:21'),
            _row('000823', '超声电子', 3, '5天3板'),
            _row('002201', '九鼎新材', 2, '2连板'),
            _row('603938', '三孚股份', 2, '4天2板', '烂2'),
            _row('002161', '远望谷', 1, '首板', '一'),
            _row('000070', '特发信息', 0, '', '炸', '00:00'),
        ],
        '803023': [_row('300563', '神宇股份', 1, '首板', plate='803023')],
    },
    'plate_stocks_zb': {
        '801660': [{'stock_code': '000070', 'stock_name': '特发信息',
                    'up_limit_type': '炸', 'up_limit_time': '00:00',
                    'up_limit_desc': '', 'up_limit_keep_times': 0,
                    'plate_code': '801660'}],
        '803023': [],
    },
    'plate_stocks_bx': {'801660': [], '803023': []},
    # 全市场 csv 序 ≠ 板块行序（原站 stocks 以全市场序为准取保序子集）
    'stocks': '000070,000823,002161,603421,002201,603938,300563',
    'max_count': 3,
    'relay': {'area': []},
    'plate_stocks_next_yi': {},
    'stock_info': {},
    'stocks_hot': {'000823': 13, '002161': 21, '000070': 23, '603421': 7},
    'stocks_hot_n': 20,
    'ban_info': {},
}


class DeriveBoardTests(unittest.TestCase):
    def setUp(self):
        self.got = _derive_board_from_snapshot(SNAP, '801660', '2026-09-11')

    def test_key_order_matches_origin(self):
        self.assertEqual(list(self.got.keys()), KEY_ORDER)

    def test_ban_info_counts_by_keep_times_with_zero_tier(self):
        # kt: 3,3,2,2,1 -> {1:1, 2:2, 3:2}，max_count=3
        self.assertEqual(self.got['ban_info'],
                         {'1': {'count': 1}, '2': {'count': 2}, '3': {'count': 2}})
        self.assertEqual(self.got['max_count'], 3)

    def test_relay_counts_true_lianban_only(self):
        # ban_n>=2 只计 desc=='{n}连板'：'5天3板'/'4天2板' 排除
        area = self.got['relay']['area']
        self.assertEqual(area, [
            {'p_code': '801660', 'p_score': 6609, 'count': 1, 'ban_n': 1},
            {'p_code': '801660', 'p_score': 6609, 'count': 1, 'ban_n': 2},
            {'p_code': '801660', 'p_score': 6609, 'count': 1, 'ban_n': 3},
        ])

    def test_stocks_csv_is_global_order_subset(self):
        self.assertEqual(self.got['stocks'],
                         '000070,000823,002161,603421,002201,603938')
        row_codes = [r['stock_code'] for r in self.got['plate_stocks']['801660']]
        self.assertNotEqual(self.got['stocks'].split(','), row_codes)

    def test_stock_info_and_hot_follow_row_order(self):
        row_codes = [r['stock_code'] for r in self.got['plate_stocks']['801660']]
        self.assertEqual(list(self.got['stock_info'].keys()), row_codes)
        self.assertEqual(list(self.got['stocks_hot'].keys()), row_codes)
        self.assertTrue(all(v == {'plates': ['通信']}
                            for v in self.got['stock_info'].values()))
        self.assertEqual(set(self.got['stocks_hot'].values()), {1})

    def test_stocks_hot_n_counts_global_hot_ge_20(self):
        # 002161(21) 与炸板行 000070(23) 计入；000823(13)/603421(7) 不计
        self.assertEqual(self.got['stocks_hot_n'], 2)

    def test_plate_and_info_and_rows_from_snapshot(self):
        self.assertEqual(self.got['plate'], [['通信', '801660', 6609]])
        self.assertEqual(self.got['plate_info'],
                         {'801660': {'score': 6609, 'name': '通信', 'code': '801660'}})
        self.assertEqual(self.got['plate_stocks']['801660'],
                         SNAP['plate_stocks']['801660'])
        self.assertEqual(self.got['plate_stocks_zb']['801660'],
                         SNAP['plate_stocks_zb']['801660'])
        self.assertEqual(self.got['plate_stocks_bx'], {'801660': []})
        self.assertEqual(self.got['plate_stocks_next_yi'], {})
        self.assertFalse(self.got['today'])

    def test_untracked_board_empty_template(self):
        got = _derive_board_from_snapshot(SNAP, '801001', '2026-09-11')
        self.assertEqual(list(got.keys()), KEY_ORDER)
        self.assertEqual(got, {
            'today': False, 'plate': [], 'plate_info': {}, 'plate_stocks': {},
            'plate_stocks_zb': {}, 'plate_stocks_bx': {}, 'stocks': '',
            'max_count': 1, 'relay': {'area': []}, 'plate_stocks_next_yi': {},
            'stock_info': {}, 'stocks_hot': {}, 'stocks_hot_n': [],
            'ban_info': {'1': {'count': 0}},
        })

    def test_all_broken_board_floor_max_count(self):
        snap = {'plate_stocks': {'801660': [_row('000070', '特发信息', 0, '', '炸', '00:00')]},
                'plate_stocks_zb': {'801660': []}, 'plate_info': {}, 'stocks': '000070',
                'stocks_hot': {}}
        got = _derive_board_from_snapshot(snap, '801660', '2026-09-11')
        self.assertEqual(got['max_count'], 1)
        self.assertEqual(got['ban_info'], {'1': {'count': 0}})
        self.assertEqual(got['stocks'], '000070')


class StoreRoundtripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'test.db'))

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_get_latest(self):
        self.assertIsNone(self.db.review_uplimit_get('2026-09-11'))
        self.db.review_uplimit_save('2026-09-10', {'plate': [], 'plate_stocks': {}})
        self.db.review_uplimit_save('2026-09-11', SNAP)
        self.assertEqual(self.db.review_uplimit_get('2026-09-11'), SNAP)
        self.assertEqual(self.db.review_uplimit_latest()[0], '2026-09-11')
        self.assertEqual(self.db.review_uplimit_latest(before='2026-09-10')[0], '2026-09-10')
        self.assertIsNone(self.db.review_uplimit_latest(before='2026-09-01'))

    def test_seed_file_valid(self):
        seed = Path(__file__).resolve().parent.parent / 'app' / 'datasources' / 'review_uplimit_seed.json'
        if not seed.exists():
            self.skipTest('seed not present')
        import json
        data = json.loads(seed.read_text(encoding='utf-8'))
        self.assertGreaterEqual(len(data), 1)
        for day, payload in data.items():
            self.assertRegex(day, r'^\d{4}-\d{2}-\d{2}$')
            self.assertIsInstance(payload.get('plate'), list)
            self.assertIsInstance(payload.get('plate_stocks'), dict)


class RouteSnapshotPriorityTests(unittest.TestCase):
    def test_snapshot_hit_returns_derived_matrix(self):
        fake = MagicMock()
        fake.review_uplimit_get.return_value = SNAP
        with patch('app.api.open_routes.store', fake), \
                patch('app.services.pools.published', return_value=None), \
                patch('app.config.settings.collector_mode', 'external'):
            resp = asyncio.run(review_uplimit_hot(date1='2026-09-11', board='801660'))
        fake.review_uplimit_get.assert_called_once_with('2026-09-11')
        self.assertEqual(resp['code'], 20000)
        self.assertEqual(resp['data'], _derive_board_from_snapshot(SNAP, '801660', '2026-09-11'))

    def test_snapshot_miss_falls_back_to_local(self):
        fake = MagicMock()
        fake.review_uplimit_get.return_value = None
        sentinel = {'today': False, 'plate': [], 'fallback': True}
        with patch('app.api.open_routes.store', fake), \
                patch('app.services.pools.published', return_value=None), \
                patch('app.config.settings.collector_mode', 'external'), \
                patch('app.api.open_routes._uplimit_hot_board', return_value=sentinel) as fb:
            resp = asyncio.run(review_uplimit_hot(date1='2026-09-11', board='801001'))
        self.assertEqual(resp['data'], sentinel)
        self.assertEqual(fb.call_args[0][0], '801001')


if __name__ == '__main__':
    unittest.main()
