import tempfile
import unittest
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.core.store import Store
from app.services import plate_flow


class PlateFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'db.sqlite'))

    def tearDown(self):
        self.tmp.cleanup()

    def test_plate_members_full_respects_captured_at_and_real_json_shape(self):
        """全量成分快照不得进入其采集日前的历史日期；真实 JSON 结构可解析。"""
        raw = json.loads(plate_flow.FULL_MEMBERS_PATH.read_text(encoding='utf-8'))
        self.assertIsInstance(raw.get('plates'), dict)
        self.assertTrue(raw['plates'])
        for value in raw['plates'].values():
            self.assertRegex(value.get('captured_at', ''), r'^\d{4}-\d{2}-\d{2}$')
            self.assertIsInstance(value.get('stocks'), list)
            self.assertTrue(all(isinstance(code, str) and code for code in value['stocks']))

        historical = {'801660': {
            'sub_plates': [{'code': '801661', 'name': 'A'}],
            'stocks': {'2026-09-10': {'801661': ['600001']}},
        }}
        full = {'801660': {'captured_at': '2026-09-11', 'stocks': ['600002']}}
        with patch.object(plate_flow, 'load_members', return_value=historical), \
                patch.object(plate_flow, 'load_full_members', return_value=full):
            before = plate_flow.plate_members_full('801660', '2026-09-10')
            on_capture = plate_flow.plate_members_full('801660', '2026-09-11')
        self.assertEqual(before, {'600001'})
        self.assertEqual(on_capture, {'600001', '600002'})

    def test_rank_days_reads_older_history_window(self):
        """date2 指向较旧日期时，仍能读取该日之前所需的 N 个快照日。"""
        for day_num in range(20, 32):
            day = f'2026-08-{day_num:02d}'
            self.db.plate_rank_save(17, day, [
                self._plate_row('801660', '通信', day, day_num * 100, day_num, day_num * 10),
            ])
        with patch.object(plate_flow, 'store', self.db):
            rows = asyncio.run(plate_flow.rank_days('2026-08-22', 3, 1, 3, limit=10))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['days'], 3)
        self.assertEqual(rows[0]['sum_rate'], 63.0)
        self.assertEqual(rows[0]['sum_leader_money'], 6300.0)
        self.assertEqual(rows[0]['sum_score'], 630.0)

    def test_rank_days_does_not_use_unlabelled_stock_flow_fallback(self):
        """无板块快照时不输出伪装成原站 sum_rate/sum_score 的个股聚合。"""
        with patch.object(plate_flow, 'store', self.db), \
                patch.object(self.db, 'stock_flow_dates', side_effect=AssertionError('fallback disabled')):
            rows = asyncio.run(plate_flow.rank_days(None, 1, 1, 3, limit=10))
        self.assertEqual(rows, [])

    def test_pct_schemes_differ_by_window(self):
        """累计 5/10/20 日门槛与档位不同；同涨幅落入不同桶。"""
        self.assertEqual(plate_flow._pct_scheme(5), (10.0, ["10-15", "15-20", "20-30", "30-40", "40+"]))
        self.assertEqual(plate_flow._pct_scheme(10), (20.0, ["20-40", "40-60", "60-80", "80-100", "100+"]))
        self.assertEqual(plate_flow._pct_scheme(20), (30.0, ["30-50", "50-80", "80-100", "100-150", "150+"]))
        self.assertEqual(plate_flow._pct_scheme(1), plate_flow._pct_scheme(10))  # 未知窗口回退 10 日
        self.assertEqual(plate_flow._interval_of(45, 5), "40+")
        self.assertEqual(plate_flow._interval_of(45, 10), "40-60")
        self.assertEqual(plate_flow._interval_of(45, 20), "30-50")
        self.assertIsNone(plate_flow._interval_of(12, 10))
        self.assertEqual(plate_flow._interval_of(12, 5), "10-15")
        self.assertIsNone(plate_flow._interval_of(25, 20))

    def test_stocks_pct_intervals_and_formula(self):
        """累计涨幅：close/close-1 基期、10 日方案分桶、桶内降序。days=1 回退 10 日档。"""
        members = {'plates': {'801660': {
            'sub_plates': [{'code': '801661', 'name': 'PCB'}],
            'stocks': {'2026-09-11': {'801661': ['600001', '600002', '600003', '600004']}},
        }}}
        self.db.daily_close_save('2026-09-10', [
            {'stock_code': '600001', 'close': 10.0, 'prev_close': 9.5},
            {'stock_code': '600002', 'close': 10.0, 'prev_close': 10.0},
            {'stock_code': '600003', 'close': 10.0, 'prev_close': 10.0},
            {'stock_code': '600004', 'close': 10.0, 'prev_close': 10.0},
        ], {'complete': True, 'trade_date': '2026-09-10'})
        self.db.daily_close_save('2026-09-11', [
            {'stock_code': '600001', 'stock_name': '甲', 'close': 13.0, 'prev_close': 12.0},
            {'stock_code': '600002', 'stock_name': '乙', 'close': 15.0, 'prev_close': 14.0},  # +50%
            {'stock_code': '600003', 'stock_name': '丙', 'close': 11.0, 'prev_close': 10.0},  # +10% -> excluded
            {'stock_code': '600004', 'stock_name': '丁', 'close': 21.0, 'prev_close': 20.0},  # +110% -> 100+
        ], {'complete': True, 'trade_date': '2026-09-11'})
        with patch.object(plate_flow, 'store', self.db), \
                patch.object(plate_flow, 'load_members', return_value=members['plates']):
            data = plate_flow.stocks_pct('801660', '2026-09-11', 1)
        self.assertEqual(data['intervals'], ['20-40', '40-60', '60-80', '80-100', '100+'])
        self.assertEqual(data['meta']['min_pct'], 20.0)
        by_code = {iv: {s['stock_code']: s['cum_pct'] for s in arr}
                   for iv, arr in data['stocks'].items()}
        self.assertEqual(by_code['20-40']['600001'], 30.0)
        self.assertEqual(by_code['40-60']['600002'], 50.0)
        self.assertEqual(by_code['100+']['600004'], 110.0)
        self.assertNotIn('600003', by_code['20-40'])
        self.assertEqual(by_code['60-80'], {})

    def test_qfq_base_close_cash_dividend(self):
        """跨除息窗口等比前复权：通达创智 10 派 4 → 与原站 40.55 一致。"""
        rec = {
            'verified': True, 'complete': True,
            'events': [{'ex_date': '2026-09-23', 'cash_ps': 0.4, 'kind': 'cash',
                        'verified': True, 'complete': True}],
        }
        with patch.object(plate_flow.store, 'kv_get', return_value=rec):
            adj, factor = plate_flow._qfq_base_close(
                '001368', 27.35, '2026-09-10', '2026-09-24',
                closes_by_date={'2026-09-22': 34.84, '2026-09-10': 27.35},
            )
        self.assertAlmostEqual(factor, (34.84 - 0.4) / 34.84, places=6)
        self.assertAlmostEqual(round((38.0 / adj - 1) * 100, 2), 40.55)

    def _plate_row(self, code, name, date, money, rate, score):
        return {
            'date1': date, 'plate_code': code, 'plate_name': name, 'plate_type': 17,
            'score': score, 'money_leader': money, 'money_leader_buy': 0, 'money_leader_sell': 0,
            'trade_money': 1e9, 'market_cap_cir': 1e10, 'rate': rate, 'speed': 1.2,
            'volume_ration': 1.5, 'time': date + ' 18:01:00',
        }

    def test_rank_days_prefers_plate_snapshot(self):
        self.db.plate_rank_save(17, '2026-09-11', [
            self._plate_row('801660', '通信', '2026-09-11', 25.47e8, -0.82, 6609),
            self._plate_row('801445', '元器件', '2026-09-11', 34.70e8, 1.10, 1631),
        ])
        with patch.object(plate_flow, 'store', self.db):
            rows = asyncio.run(plate_flow.rank_days(None, 1, 1, 3, limit=10))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['plate_code'], '801445')  # money sort
        self.assertAlmostEqual(rows[0]['sum_leader_money'], 34.70e8, delta=1.0)
        self.assertEqual(rows[0]['plate_name'], '元器件')
        # strength sort
        with patch.object(plate_flow, 'store', self.db):
            rows = asyncio.run(plate_flow.rank_days(None, 1, 1, 9, limit=10))
        self.assertEqual(rows[0]['plate_code'], '801660')
        # 原站 last_day 为全零占位（plate_code "0"）
        self.assertEqual(rows[0]['last_day']['plate_code'], '0')
        self.assertEqual(rows[0]['last_day']['rate'], 0)
        self.assertEqual(rows[0]['last_day']['money_leader'], 0)

    def test_rank_columns_keeps_each_saved_day(self):
        self.db.plate_rank_save(17, '2026-09-18', [self._plate_row('801001', '芯片', '2026-09-18', 1, 2.467, 12560)])
        self.db.plate_rank_save(17, '2026-09-21', [self._plate_row('801045', '医药', '2026-09-21', 2, 3.6, 12646)])
        self.db.plate_rank_day_meta_save(17, '2026-09-18', status='final', source='t', collected_at=None, row_count=1, fingerprint='a')
        self.db.plate_rank_day_meta_save(17, '2026-09-21', status='final', source='t', collected_at=None, row_count=1, fingerprint='b')
        with patch.object(plate_flow, 'store', self.db), patch('app.services.plate_rank_refresh.store', self.db):
            columns = plate_flow.rank_columns('2026-09-21', 15, 1, 9, limit=12)
        self.assertEqual([c['date'] for c in columns], ['2026-09-21', '2026-09-18'])
        self.assertEqual(columns[0]['rows'][0]['plate_code'], '801045')
        self.assertEqual(columns[1]['status'], 'final')

    def test_rank_days_multi_day_aggregates(self):
        self.db.plate_rank_save(17, '2026-09-10', [self._plate_row('801660', '通信', '2026-09-10', 10e8, 1.0, 100)])
        self.db.plate_rank_save(17, '2026-09-11', [self._plate_row('801660', '通信', '2026-09-11', 20e8, 3.0, 200)])
        with patch.object(plate_flow, 'store', self.db):
            rows = asyncio.run(plate_flow.rank_days(None, 2, 1, 3, limit=10))
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]['sum_leader_money'], 30e8, delta=1.0)
        # 原站多日口径：区间求和（rate 1.0+3.0=4.0，score 100+200=300）
        self.assertAlmostEqual(rows[0]['sum_rate'], 4.0, delta=0.01)
        self.assertEqual(rows[0]['sum_score'], 300)
        self.assertEqual(rows[0]['days'], 2)

    def test_rank_days_falls_back_when_no_snapshot(self):
        # No plate_rank_daily: the unlabelled stock-flow fallback is intentionally disabled.
        with patch.object(plate_flow, 'store', self.db), \
                patch.object(plate_flow, 'load_members', return_value={}):
            rows = asyncio.run(plate_flow.rank_days(None, 1, 1, 3, limit=10))
        self.assertEqual(rows, [])

    def test_rank_days_supports_all_plate_types(self):
        # 15/14 类与 17 类同一聚合口径，各自读自己的快照表分区
        for pt, code in ((15, '801445'), (14, '801016')):
            self.db.plate_rank_save(pt, '2026-09-11', [
                self._plate_row(code, '测试板块', '2026-09-11', 10e8, 2.0, 500),
            ])
        with patch.object(plate_flow, 'store', self.db):
            rows15 = asyncio.run(plate_flow.rank_days(None, 1, 1, 3, limit=10, plate_type=15))
            rows14 = asyncio.run(plate_flow.rank_days(None, 1, 1, 3, limit=10, plate_type=14))
        self.assertEqual([r['plate_code'] for r in rows15], ['801445'])
        self.assertEqual([r['plate_code'] for r in rows14], ['801016'])
        self.assertAlmostEqual(rows15[0]['sum_leader_money'], 10e8, delta=1.0)

    def test_collect_plate_rank_saves_three_types(self):
        rows17 = [self._plate_row('801660', '通信', '2026-09-11', 2546550000, 1, 1)]
        rows15 = [dict(rows17[0], plate_type=15, plate_code='801445', plate_name='元器件')]
        rows14 = [dict(rows17[0], plate_type=14, plate_code='801016', plate_name='白色家电')]
        async def fake_rank(pt, date1, limit=500):
            return {17: rows17, 15: rows15, 14: rows14}[pt]
        async def fake_rank_days(pt, date2, n_days=1, limit=500):
            code = {17: '801660', 15: '801445', 14: '801016'}[pt]
            return [{'plate_code': code, 'plate_name': 'x', 'sum_rate': 1.0,
                     'sum_leader_money': 2546553856.0, 'sum_score': 1.0, 'days': 1,
                     'last_day': {'plate_code': '0', 'plate_name': '', 'rate': 0, 'money_leader': 0, 'score': 0}}]

        from app.datasources import zizizaizai
        with patch.object(plate_flow, 'store', self.db), \
                patch.object(zizizaizai, 'plates_rank', side_effect=fake_rank), \
                patch.object(zizizaizai, 'plates_rank_days', side_effect=fake_rank_days):
            data = asyncio.run(plate_flow.collect_plate_rank('2026-09-11'))
        self.assertEqual(data['plates'], {'17': 1, '15': 1, '14': 1})
        self.assertEqual(data['exact'], {'17': 1, '15': 1, '14': 1})
        self.assertEqual(self.db.plate_rank_dates(17, limit=5), ['2026-09-11'])

    def test_rank_days_prefers_exact_money(self):
        """sum_leader_money 优先全精度列；payload(/rank 口径) 不被污染。"""
        self.db.plate_rank_save(17, '2026-09-11', [
            self._plate_row('801660', '通信', '2026-09-11', 2546550000, -0.82, 6609),
        ])
        self.db.plate_rank_exact_save(17, '2026-09-11', {'801660': 2546553856.0})
        with patch.object(plate_flow, 'store', self.db):
            rows = asyncio.run(plate_flow.rank_days(None, 1, 1, 3, limit=10))
        self.assertEqual(rows[0]['sum_leader_money'], 2546553856.0)
        # 默认 range（/rank 等透传接口用）不携带 exact 字段，money_leader 保持舍入值
        snap = self.db.plate_rank_range(17, '2026-09-11', '2026-09-11')
        self.assertEqual(snap[0]['money_leader'], 2546550000)
        self.assertNotIn('money_leader_exact', snap[0])
        merged = self.db.plate_rank_range(17, '2026-09-11', '2026-09-11', with_exact=True)
        self.assertEqual(merged[0]['money_leader_exact'], 2546553856.0)

    def test_sub_plates_stocks_structure_and_stats(self):
        """子板块筛选条：结构 + 原站口径（quote_rate 剔除北交所与无行情成分；
        涨跌停 = close 与涨跌停价严格相等，r 按代码 30%/20%/10%，不依赖东财池）。"""
        members = {'801660': {
            'sub_plates': [{'code': '801003', 'name': '5G'}, {'code': '801206', 'name': '光模块'}],
            'stocks': {'2026-09-11': {
                '801003': ['000008', '000016', '832000'],
                '801206': ['301139', '301390', '000063'],
            }},
        }}
        # 涨跌停池不再参与计数：存与价格规则矛盾的脏数据，验证独立性
        self.db.limit_pool_save('em_up', '2026-09-11', {'complete': True, 'date': '2026-09-11',
                                                        'total': 1, 'pool': [{'c': '000063'}]})
        self.db.limit_pool_save('em_down', '2026-09-11', {'complete': True, 'date': '2026-09-11',
                                                          'total': 1, 'pool': [{'c': '000008'}]})
        self.db.daily_close_save('2026-09-11', [
            {'stock_code': '000008', 'close': 11.0, 'prev_close': 10.0},   # +10% = 涨停价 11.00
            {'stock_code': '832000', 'close': 10.5, 'prev_close': 10.0},   # 北交所：均值剔除；涨停价 13.00 未触及
            {'stock_code': '301139', 'close': 12.0, 'prev_close': 10.0},   # 创业板 +20% = 涨停价 12.00
            {'stock_code': '301390', 'close': 9.0, 'prev_close': 10.0},    # -10% ≠ 创业板跌停价 8.00，不计
            {'stock_code': '000063', 'close': 20.0, 'prev_close': 20.0},   # 0%
        ], {'complete': True})
        with patch.object(plate_flow, 'store', self.db), \
                patch.object(plate_flow, 'load_members', lambda: members):
            data = plate_flow.sub_plates_stocks('801660', ['2026-09-11'])
        self.assertEqual([s['code'] for s in data['sub_plates']], ['801003', '801206'])
        stocks = data['stocks']['2026-09-11']
        self.assertEqual(stocks['801003'], ['000008', '000016', '832000'])
        stats = data['stats']['2026-09-11']
        # 801003: 000008 +10%；832000 北交所剔除、000016 无行情剔除 -> 均值 10%
        self.assertEqual(stats['801003'], {'quote_rate': 10.0, 'limit_up_count': 1, 'limit_down_count': 0})
        # 801206: +20%、-10%、0% -> 均值 3.33；涨停 1（301139），301390 未触及跌停价不计，
        # em_up 池中的 000063 也不计（池已不参与）
        self.assertEqual(stats['801206']['quote_rate'], 3.33)
        self.assertEqual(stats['801206']['limit_up_count'], 1)
        self.assertEqual(stats['801206']['limit_down_count'], 0)

    def test_sub_plates_stocks_unknown_plate(self):
        with patch.object(plate_flow, 'load_members', lambda: {}):
            data = plate_flow.sub_plates_stocks('999999', ['2026-09-11'])
        # 原站口径：stocks 按请求日期返回空 dict，无子板块时不带 stats 字段
        self.assertEqual(data, {'sub_plates': [], 'stocks': {'2026-09-11': {}}})

    def test_sub_plates_stocks_per_date_membership_and_em_mapped(self):
        """按日期取各自成分快照（不复用最新），EM_MAPPED 不出现在响应中。"""
        members = {
            '801660': {
                'sub_plates': [{'code': '801099', 'name': '覆铜板'}],
                'stocks': {
                    '2026-09-07': {'801099': ['000823', '002141']},
                    '2026-09-11': {'801099': ['000823', '002141', '605198']},
                },
            },
            '801358': {
                'sub_plates': [],
                'stocks': {'2026-09-11': {'EM_MAPPED': ['600000', '000001']}},
            },
        }
        self.db.daily_close_save('2026-09-07', [
            {'stock_code': '000823', 'close': 11.0, 'prev_close': 10.0},
            {'stock_code': '002141', 'close': 10.0, 'prev_close': 10.0},
        ], {'complete': True})
        self.db.daily_close_save('2026-09-11', [
            {'stock_code': '000823', 'close': 11.0, 'prev_close': 10.0},
            {'stock_code': '002141', 'close': 10.0, 'prev_close': 10.0},
            {'stock_code': '605198', 'close': 10.0, 'prev_close': 10.0},
        ], {'complete': True})
        with patch.object(plate_flow, 'store', self.db), \
                patch.object(plate_flow, 'load_members', lambda: members):
            data = plate_flow.sub_plates_stocks('801660', ['2026-09-07', '2026-09-11'])
        self.assertEqual(data['stocks']['2026-09-07']['801099'], ['000823', '002141'])
        self.assertEqual(data['stocks']['2026-09-11']['801099'], ['000823', '002141', '605198'])
        # 09-07 均值只按当日 2 只成分：(10% + 0%) / 2 = 5%
        self.assertEqual(data['stats']['2026-09-07']['801099']['quote_rate'], 5.0)
        with patch.object(plate_flow, 'load_members', lambda: members):
            data2 = plate_flow.sub_plates_stocks('801358', ['2026-09-11'])
        self.assertEqual(data2, {'sub_plates': [], 'stocks': {'2026-09-11': {}}})

    def test_sub_plates_stocks_excludes_listing_day_from_quote_rate(self):
        """首日新股（N 前缀名 + 无早于当日的日线记录）从 quote_rate 剔除；
        成分列表与涨跌停计数不受影响（原站 801881/688801 2026-09-11 精确验证）。"""
        members = {'801085': {
            'sub_plates': [{'code': '801881', 'name': '算力'}],
            'stocks': {'2026-09-11': {'801881': ['600001', '688801']}},
        }}
        self.db.daily_close_save('2026-09-10', [
            {'stock_code': '600001', 'stock_name': '甲股', 'close': 10.0, 'prev_close': 10.0},
        ], {'complete': True})
        self.db.daily_close_save('2026-09-11', [
            {'stock_code': '600001', 'stock_name': '甲股', 'close': 9.0, 'prev_close': 10.0},
            {'stock_code': '688801', 'stock_name': 'N某某', 'close': 397.0, 'prev_close': 142.18},
        ], {'complete': True})
        with patch.object(plate_flow, 'store', self.db), \
                patch.object(plate_flow, 'load_members', lambda: members):
            data = plate_flow.sub_plates_stocks('801085', ['2026-09-11'])
        stats = data['stats']['2026-09-11']['801881']
        # 首日新股 +179% 被剔除，均值只含 600001 的 -10%
        self.assertEqual(stats['quote_rate'], -10.0)
        # 600001 恰为跌停价 9.00 计入；688801 close != 涨停价(170.62) 不计
        self.assertEqual(stats['limit_down_count'], 1)
        self.assertEqual(stats['limit_up_count'], 0)
        # 成分列表保持原样
        self.assertEqual(data['stocks']['2026-09-11']['801881'], ['600001', '688801'])

    def test_rates_and_rank_use_real_listing_day_prev_close(self):
        """首日新股必须使用日线真实 prev_close；原站 1.00 占位造成的 39600% 已废弃。"""
        members = {'801085': {
            'sub_plates': [{'code': '801881', 'name': '算力'}],
            'stocks': {'2026-09-11': {'801881': ['600001', '688801']}},
        }}
        self.db.daily_close_save('2026-09-10', [
            {'stock_code': '600001', 'stock_name': '甲股', 'close': 10.0, 'prev_close': 10.0},
        ], {'complete': True})
        self.db.daily_close_save('2026-09-11', [
            {'stock_code': '600001', 'stock_name': '甲股', 'close': 9.0, 'prev_close': 10.0},
            {'stock_code': '688801', 'stock_name': 'N某某', 'close': 397.0, 'prev_close': 142.18},
        ], {'complete': True})
        with patch.object(plate_flow, 'store', self.db), \
                patch.object(plate_flow, 'load_members', lambda: members):
            data = plate_flow.sub_plate_stocks_rates('801881', '2026-09-11', 1, 30)
        self.assertEqual([s['stock_code'] for s in data['list']], ['688801', '600001'])
        self.assertEqual(data['list'][0]['px_change_rate'], 179.224)
        self.assertEqual(data['list'][0]['last_px'], 397.0)
        self.assertEqual(data['list'][0]['data_status'], 'ok')
        self.assertEqual(data['list'][1]['px_change_rate'], -10.0)
        # pt=17 全成分版同口径
        with patch.object(plate_flow, 'store', self.db), \
                patch.object(plate_flow, 'load_members', lambda: members):
            data = plate_flow.plate_stocks_rates('801085', '2026-09-11', 1, 30)
        self.assertEqual(data['list'][0]['px_change_rate'], 179.224)
        # rank/list with_pct 同口径
        pop_snap = {'date': '2026-09-11', 'complete': True, 'total': 2, 'items': [
            {'symbol_code': '688801', 'symbol_name': '', 'rank': 14, 'rank_diff': 1017, 'heat': 2142700},
            {'symbol_code': '600001', 'symbol_name': '', 'rank': 500, 'rank_diff': 0, 'heat': 100},
        ]}
        import app.services.popular as popular_svc
        with patch.object(plate_flow, 'store', self.db), \
                patch.object(plate_flow, 'load_members', lambda: members), \
                patch.object(popular_svc, 'published', return_value=pop_snap):
            data = asyncio.run(plate_flow.plate_stocks_popular_rank('801881', '2026-09-11', 1, 30, 1, plate_type=18))
        self.assertEqual([s['stock_code'] for s in data['list']], ['688801', '600001'])
        self.assertEqual(data['list'][0]['px_change_rate'], 179.224)

    def test_sub_plate_stocks_rates_formula_and_missing_status(self):
        """个股涨幅榜：真实公式、6 位有效数字、缺涨幅置后且区分当日缺行情/缺失。"""
        members = {'801660': {
            'sub_plates': [{'code': '801661', 'name': 'PCB'}],
            'stocks': {'2026-09-11': {'801661': ['600001', '600002', '600003', '600004', '600005']}},
        }}
        # 600004 只有 <=09-09 的历史日线，不能据此断言停牌
        self.db.daily_close_save('2026-09-09', [
            {'stock_code': '600004', 'stock_name': '旧价股', 'close': 8.0, 'prev_close': 7.9},
        ], {'complete': True, 'trade_date': '2026-09-09'})
        self.db.daily_close_save('2026-09-11', [
            # 27.92/25.11-1 = 11.1907...% -> 6 位有效数字 11.1908
            {'stock_code': '600001', 'stock_name': '甲', 'close': 27.92, 'prev_close': 25.11},
            {'stock_code': '600002', 'stock_name': '乙', 'close': 10.0, 'prev_close': 9.0},  # 11.1111
            {'stock_code': '600003', 'stock_name': '丙', 'close': 9.0, 'prev_close': 10.0},  # -10.0
        ], {'complete': True, 'trade_date': '2026-09-11'})
        with patch.object(plate_flow, 'store', self.db), \
                patch.object(plate_flow, 'load_members', lambda: members):
            data = plate_flow.sub_plate_stocks_rates('801661', '2026-09-11', 1, 30)
        self.assertEqual(data['total'], 5)
        self.assertEqual([s['stock_code'] for s in data['list']],
                         ['600001', '600002', '600003', '600004', '600005'])
        self.assertAlmostEqual(data['list'][0]['px_change_rate'], 11.1908, places=5)
        self.assertEqual(data['list'][1]['px_change_rate'], 11.1111)
        # 无行情不再伪造 0 涨幅：有旧收盘但无当日行情，不能断言停牌
        self.assertIsNone(data['list'][3]['px_change_rate'])
        self.assertEqual(data['list'][3]['last_px'], 8.0)
        self.assertEqual(data['list'][3]['stock_name'], '旧价股')
        self.assertEqual(data['list'][3]['data_status'], 'missing_current_quote')
        self.assertIsNone(data['list'][4]['px_change_rate'])
        self.assertIsNone(data['list'][4]['last_px'])
        self.assertEqual(data['list'][4]['data_status'], 'missing')

    def test_sub_plate_stocks_rates_pagination(self):
        """分页：page/limit 切片、无重叠、未知子板块空返回。"""
        stocks = [f'60000{i}' for i in range(1, 8)]
        members = {'801660': {
            'sub_plates': [{'code': '801661', 'name': 'PCB'}],
            'stocks': {'2026-09-11': {'801661': stocks}},
        }}
        self.db.daily_close_save('2026-09-11', [
            {'stock_code': c, 'stock_name': f'股{c}', 'close': 10.0 + i, 'prev_close': 10.0}
            for i, c in enumerate(stocks)
        ], {'complete': True, 'trade_date': '2026-09-11'})
        with patch.object(plate_flow, 'store', self.db), \
                patch.object(plate_flow, 'load_members', lambda: members):
            p1 = plate_flow.sub_plate_stocks_rates('801661', '2026-09-11', 1, 3)
            p3 = plate_flow.sub_plate_stocks_rates('801661', '2026-09-11', 3, 3)
            empty = plate_flow.sub_plate_stocks_rates('999999', '2026-09-11', 1, 30)
        self.assertEqual(p1['total'], 7)
        self.assertEqual([s['stock_code'] for s in p1['list']], ['600007', '600006', '600005'])
        self.assertEqual(p3['list'][0]['stock_code'], '600001')
        self.assertEqual(empty, {'list': [], 'total': 0, 'page': 1, 'limit': 30})

    def test_plate_stocks_rates_union_and_formula(self):
        """plates/17 版：全部子板块成分并集、同公式、缺涨幅置后。"""
        members = {'801660': {
            'sub_plates': [{'code': '801661', 'name': 'A'}, {'code': '801662', 'name': 'B'}],
            'stocks': {'2026-09-11': {
                '801661': ['600001', '600002'],
                '801662': ['600002', '600003', '600004'],  # 600002 跨子板块并集去重
            }},
        }}
        self.db.daily_close_save('2026-09-09', [
            {'stock_code': '600003', 'stock_name': '旧价股', 'close': 5.0, 'prev_close': 4.9},
        ], {'complete': True, 'trade_date': '2026-09-09'})
        self.db.daily_close_save('2026-09-11', [
            {'stock_code': '600001', 'stock_name': '甲', 'close': 11.0, 'prev_close': 10.0},
            {'stock_code': '600002', 'stock_name': '乙', 'close': 10.0, 'prev_close': 10.0},
        ], {'complete': True, 'trade_date': '2026-09-11'})
        with patch.object(plate_flow, 'store', self.db), \
                patch.object(plate_flow, 'load_members', lambda: members):
            data = plate_flow.plate_stocks_rates('801660', '2026-09-11', 1, 30)
        # 并集 4 只（600002 去重），缺涨幅行置后
        self.assertEqual(data['total'], 4)
        self.assertEqual([s['stock_code'] for s in data['list']],
                         ['600001', '600002', '600003', '600004'])
        self.assertAlmostEqual(data['list'][0]['px_change_rate'], 10.0, places=6)
        self.assertIsNone(data['list'][2]['px_change_rate'])
        self.assertEqual(data['list'][2]['last_px'], 5.0)
        self.assertEqual(data['list'][2]['data_status'], 'missing_current_quote')
        self.assertIsNone(data['list'][3]['px_change_rate'])
        self.assertIsNone(data['list'][3]['last_px'])
        self.assertEqual(data['list'][3]['data_status'], 'missing')

    def test_plate_stocks_popular_rank_top300(self):
        """板块成分人气榜：成分 ∩ 人气快照、rank 升序、top300 截断。"""
        members = {'801660': {
            'sub_plates': [{'code': '801661', 'name': 'A'}],
            'stocks': {'2026-09-11': {'801661': ['600001', '600002', '600003']}},
        }}
        pop_items = [
            {'symbol_code': '600002', 'symbol_name': '乙', 'rank': 2, 'rank_diff': 1, 'heat': 500.0},
            {'symbol_code': '600001', 'symbol_name': '甲', 'rank': 5, 'rank_diff': 0, 'heat': 300.0},
        ]
        pop_snap = {'date': '2026-09-11', 'complete': True, 'total': 2, 'items': pop_items}
        self.db.daily_close_save('2026-09-11', [
            {'stock_code': '600001', 'stock_name': '甲', 'close': 11.0, 'prev_close': 10.0},
            {'stock_code': '600002', 'stock_name': '乙', 'close': 9.0, 'prev_close': 10.0},
        ], {'complete': True, 'trade_date': '2026-09-11'})
        import app.services.popular as popular_svc
        with patch.object(plate_flow, 'store', self.db), \
                patch.object(plate_flow, 'load_members', lambda: members), \
                patch.object(popular_svc, 'published', return_value=pop_snap):
            data = asyncio.run(plate_flow.plate_stocks_popular_rank('801660', '2026-09-11', 1, 30, 1))
        # 600003 不在人气快照 -> 不出现；排序按 rank 升序
        self.assertEqual(data['total'], 2)
        self.assertEqual(data['status'], 'ok')
        self.assertEqual([s['stock_code'] for s in data['list']], ['600002', '600001'])
        self.assertEqual(data['list'][0]['attention'], 500.0)
        self.assertEqual(data['list'][0]['rank_diff'], 1)
        self.assertEqual(data['list'][1]['rank_diff'], 0)
        self.assertAlmostEqual(data['list'][0]['px_change_rate'], -10.0, places=6)
        self.assertAlmostEqual(data['list'][1]['px_change_rate'], 10.0, places=6)

    def test_plate_stocks_popular_rank_keeps_missing_quote_explicit(self):
        """人气榜带涨幅时保留缺行情成分，并区分当日缺行情与未知旧价。"""
        members = {'801660': {
            'sub_plates': [{'code': '801661', 'name': 'A'}],
            'stocks': {'2026-09-11': {'801661': ['600001', '600002']}},
        }}
        self.db.daily_close_save('2026-09-09', [
            {'stock_code': '600002', 'stock_name': '乙', 'close': 8.0, 'prev_close': 8.5},
        ], {'complete': True, 'trade_date': '2026-09-09'})
        self.db.daily_close_save('2026-09-11', [
            {'stock_code': '600001', 'stock_name': '甲', 'close': 11.0, 'prev_close': 10.0},
        ], {'complete': True, 'trade_date': '2026-09-11'})
        pop_snap = {'date': '2026-09-11', 'complete': True, 'total': 2, 'items': [
            {'symbol_code': '600001', 'symbol_name': '甲', 'rank': 1, 'rank_diff': 0, 'heat': 300.0},
            {'symbol_code': '600002', 'symbol_name': '乙', 'rank': 2, 'rank_diff': 0, 'heat': 200.0},
        ]}
        import app.services.popular as popular_svc
        with patch.object(plate_flow, 'store', self.db), \
                patch.object(plate_flow, 'load_members', lambda: members), \
                patch.object(popular_svc, 'published', return_value=pop_snap):
            data = asyncio.run(plate_flow.plate_stocks_popular_rank('801660', '2026-09-11', 1, 30, 1))
        self.assertEqual(data['list'][0]['px_change_rate'], 10.0)
        self.assertEqual(data['list'][0]['data_status'], 'ok')
        self.assertIsNone(data['list'][1]['px_change_rate'])
        self.assertEqual(data['list'][1]['last_px'], 8.0)
        self.assertEqual(data['list'][1]['data_status'], 'missing_current_quote')

    def test_plate_stocks_popular_rank_no_snapshot(self):
        """无人气快照时返回空榜单，不报错。"""
        members = {'801660': {
            'sub_plates': [{'code': '801661', 'name': 'A'}],
            'stocks': {'2026-09-11': {'801661': ['600001']}},
        }}
        import app.services.popular as popular_svc
        with patch.object(plate_flow, 'store', self.db), \
                patch.object(plate_flow, 'load_members', lambda: members), \
                patch.object(popular_svc, 'published', return_value=None), \
                patch.object(popular_svc, 'em_hist_snapshot', AsyncMock(return_value=None)):
            data = asyncio.run(plate_flow.plate_stocks_popular_rank('801660', '2026-09-11', 1, 30, 1))
        self.assertEqual(data['list'], [])
        self.assertEqual(data['total'], 0)
        self.assertEqual(data['status'], 'missing_popular_snapshot')
        self.assertEqual(data['meta']['status'], 'missing_popular_snapshot')

    def test_plate_stocks_popular_rank_sub_plate_type18(self):
        """plate_type=18 子板块人气榜：成分取跨板块该子板块成分，而非 17 板块并集。"""
        members = {
            '801660': {
                'sub_plates': [{'code': '801661', 'name': 'A'}],
                'stocks': {'2026-09-11': {'801661': ['600001']}},
            },
            '801900': {
                'sub_plates': [{'code': '801661', 'name': 'A'}],
                'stocks': {'2026-09-11': {'801661': ['600002']}},
            },
        }
        pop_snap = {'date': '2026-09-11', 'complete': True, 'total': 2, 'items': [
            {'symbol_code': '600001', 'symbol_name': '甲', 'rank': 5, 'rank_diff': 0, 'heat': 300.0},
            {'symbol_code': '600002', 'symbol_name': '乙', 'rank': 2, 'rank_diff': 1, 'heat': 500.0},
        ]}
        self.db.daily_close_save('2026-09-11', [
            {'stock_code': '600002', 'stock_name': '乙', 'close': 9.0, 'prev_close': 10.0},
        ], {'complete': True, 'trade_date': '2026-09-11'})
        import app.services.popular as popular_svc
        with patch.object(plate_flow, 'store', self.db), \
                patch.object(plate_flow, 'load_members', lambda: members), \
                patch.object(popular_svc, 'published', return_value=pop_snap):
            data = asyncio.run(plate_flow.plate_stocks_popular_rank('801661', '2026-09-11', 1, 30, 1, plate_type=18))
        # 子板块 801661 成分 = {600001, 600002}（跨 801660/801900 并集）
        self.assertEqual(data['total'], 2)
        self.assertEqual([s['stock_code'] for s in data['list']], ['600002', '600001'])


    def test_plate_stocks_popular_rank_em_hist_fallback(self):
        """缺 THS 快照时用 em_hist 拼装，meta.source=em_hist。"""
        members = {'801660': {
            'sub_plates': [{'code': '801661', 'name': 'A'}],
            'stocks': {'2026-09-11': {'801661': ['600001', '600002']}},
        }}
        em_snap = {'date': '2026-09-11', 'complete': False, 'total': 2, 'source': 'em_hist',
                   'items': [
                       {'symbol_code': '600002', 'rank': 10, 'rank_diff': 2, 'attention': None},
                       {'symbol_code': '600001', 'rank': 50, 'rank_diff': None, 'attention': None},
                   ]}
        self.db.daily_close_save('2026-09-11', [
            {'stock_code': '600002', 'stock_name': '乙', 'close': 11.0, 'prev_close': 10.0},
            {'stock_code': '600001', 'stock_name': '甲', 'close': 10.0, 'prev_close': 10.0},
        ], {'complete': True, 'trade_date': '2026-09-11'})
        import app.services.popular as popular_svc
        with patch.object(plate_flow, 'store', self.db), \
                patch.object(plate_flow, 'load_members', lambda: members), \
                patch.object(popular_svc, 'published', return_value=None), \
                patch.object(popular_svc, 'em_hist_snapshot', AsyncMock(return_value=em_snap)):
            data = asyncio.run(plate_flow.plate_stocks_popular_rank('801660', '2026-09-11', 1, 30, 1))
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(data['meta']['source'], 'em_hist')
        self.assertEqual([s['stock_code'] for s in data['list']], ['600002', '600001'])

    def test_plate_reason_store_roundtrip(self):
        rows = [{
            'msg_id': '42658', 'title': '光博会', 'is_boom': 1, 'zt_num': 3, 'qd': '562',
            'created_at': '2026-09-11 17:30:00', 'plate_code': '801660',
            'boom_reason': '光博会聚焦光互联', 'date': '2026-09-10', 'lz_info': '000823,超声电子,4天2板',
        }]
        n = self.db.plate_reason_save(rows)
        self.assertEqual(n, 1)
        out = self.db.plate_reason_list('801660')
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['msg_id'], '42658')
        self.assertEqual(out[0]['zt_num'], 3)

    def test_plate_reasons_local_first_with_fallback(self):
        import asyncio
        from app.datasources import zizizaizai
        # local miss -> passthrough to origin
        async def fake_reason(code):
            return [{'msg_id': '1', 'plate_code': code, 'date': '2026-09-10', 'zt_num': 2}]
        with patch.object(plate_flow, 'store', self.db), patch.object(zizizaizai, 'plate_popular_reason', side_effect=fake_reason):
            rows = asyncio.run(plate_flow.plate_reasons('801660', limit=5))
        self.assertEqual(len(rows), 1)
        # local hit -> no origin call
        self.db.plate_reason_save([{'msg_id': '2', 'plate_code': '801660', 'date': '2026-09-11', 'zt_num': 5}])
        with patch.object(plate_flow, 'store', self.db), \
                patch.object(zizizaizai, 'plate_popular_reason', AsyncMock(side_effect=AssertionError('should not call origin'))):
            rows = asyncio.run(plate_flow.plate_reasons('801660', limit=5))
        self.assertEqual(rows[0]['msg_id'], '2')

    def test_collect_plate_reasons_saves_all_messages(self):
        import asyncio
        from app.datasources import zizizaizai
        rank_rows = [self._plate_row('801660', '通信', '2026-09-11', 25e8, -0.8, 6609)]
        msgs = [
            {'msg_id': '100', 'plate_code': '801660', 'date': '2026-09-10', 'zt_num': 3},
            {'msg_id': '99', 'plate_code': '801660', 'date': '2026-09-09', 'zt_num': 5},
        ]
        async def fake_rank(pt, date1, limit=500):
            return rank_rows
        async def fake_reason(code):
            return msgs
        with patch.object(plate_flow, 'store', self.db), \
                patch.object(zizizaizai, 'plates_rank', side_effect=fake_rank), \
                patch.object(zizizaizai, 'plate_popular_reason', side_effect=fake_reason):
            data = asyncio.run(plate_flow.collect_plate_reasons_for('2026-09-11', plate_types=(17,), top_n=15))
        self.assertEqual(data['saved'], 2)
        self.assertEqual(data['errors'], [])
        stored = self.db.plate_reason_list('801660')
        self.assertEqual({m['msg_id'] for m in stored}, {'100', '99'})


if __name__ == '__main__':
    unittest.main()
