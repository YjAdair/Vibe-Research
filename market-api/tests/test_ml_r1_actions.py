import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.store import Store
from app.services.ml_r1_actions import BASIS, return_evidence


DAY0, DAY1 = '2026-09-10', '2026-09-11'


def event(**values):
    return {'ex_date': DAY1, 'verified': True, 'complete': True, **values}


class ReturnEvidenceTests(unittest.TestCase):
    def test_verified_ordinary_day_uses_raw_previous_close(self):
        result = return_evidence(previous_close=10, close=11, official_reference=10,
                                 previous_day=DAY0, day=DAY1)
        self.assertEqual(result, {'factor': 1.1, 'basis': BASIS, 'action_day': False,
                                  'reason': None, 'raw_prev_close': 10})

    def test_cash_and_split_simple_formulas(self):
        cash = return_evidence(previous_close=10, close=9.8, official_reference=None,
                               previous_day=DAY0, day=DAY1,
                               record={'verified': True, 'complete': True,
                                       'events': [event(kind='cash', cash_ps=.2,
                                                        bonus_ps=0, convert_ps=0)]})
        self.assertAlmostEqual(cash['factor'], 1.0)
        split = return_evidence(previous_close=10, close=5, official_reference=None,
                                previous_day=DAY0, day=DAY1,
                                record={'verified': True, 'complete': True,
                                        'events': [event(kind='split', s=2)]})
        self.assertAlmostEqual(split['factor'], 1.0)

    def test_cash_total_return_and_decimal_reference(self):
        record = {'verified': True, 'complete': True,
                  'events': [event(kind='cash', cash_ps=.2)]}
        result = return_evidence(previous_close=10, close=10, official_reference=9.8,
                                 previous_day=DAY0, day=DAY1, record=record)
        self.assertAlmostEqual(result['factor'] - 1, .02)
        record['events'][0]['cash_ps'] = .125
        rounded = return_evidence(previous_close=10, close=10, official_reference=9.88,
                                  previous_day=DAY0, day=DAY1, record=record)
        self.assertIsNone(rounded['factor'])
        record.update(price_tick=.01, price_tick_verified=True, price_rounding="half_up")
        rounded = return_evidence(previous_close=10, close=10, official_reference=9.88,
                                  previous_day=DAY0, day=DAY1, record=record)
        self.assertAlmostEqual(rounded['factor'], 1.0125)

    def test_old_derived_reference_and_ambiguous_events_stay_missing(self):
        old = return_evidence(previous_close=10, close=10, official_reference=None,
                              previous_day=DAY0, day=DAY1)
        self.assertIsNone(old['factor'])
        mixed = return_evidence(previous_close=10, close=4.9, official_reference=None,
                                previous_day=DAY0, day=DAY1,
                                record={'verified': True, 'complete': True,
                                        'events': [event(cash_ps=.1, bonus_ps=1, convert_ps=0)]})
        self.assertIsNone(mixed['factor'])
        multiple = return_evidence(previous_close=10, close=10, official_reference=None,
                                   previous_day=DAY0, day=DAY1,
                                   record={'verified': True, 'complete': True,
                                           'events': [event(cash_ps=.1, bonus_ps=0, convert_ps=0),
                                                      event(cash_ps=.2, bonus_ps=0, convert_ps=0)]})
        self.assertEqual(multiple['reason'], 'multiple_events')
        incomplete = return_evidence(previous_close=10, close=9.8, official_reference=None,
                                     previous_day=DAY0, day=DAY1,
                                     record={'verified': True,
                                             'events': [{'ex_date': DAY1, 'kind': 'cash',
                                                         'cash_ps': .2, 'bonus_ps': 0,
                                                         'convert_ps': 0, 'verified': True}]})
        self.assertIsNone(incomplete['factor'])

    def test_event_reference_must_match_and_gaps_need_verified_interval(self):
        record = {'verified': True, 'complete': True,
                  'events': [event(kind='cash', cash_ps=.2, bonus_ps=0, convert_ps=0)]}
        mismatch = return_evidence(previous_close=10, close=9.8, official_reference=9.7,
                                   previous_day=DAY0, day=DAY1, record=record)
        self.assertEqual(mismatch['reason'], 'official_reference_conflict')
        gap = return_evidence(previous_close=10, close=10, official_reference=10,
                              previous_day='2026-09-09', day=DAY1)
        self.assertEqual(gap['reason'], 'incomplete_price_interval')
        halt = return_evidence(previous_close=10, close=10, official_reference=10,
                               previous_day='2026-09-09', day=DAY1,
                               record={'verified_halt': True})
        self.assertEqual(halt['basis'], BASIS)


class CollectorAndApplyTests(unittest.TestCase):
    def test_parser_records_table_scope_and_does_not_claim_all_actions(self):
        from scripts import collect_corp_actions as collector
        html = '''<table><tr><th>公告日期</th><th>送股(10送X)</th><th>转增(10转X)</th>
        <th>派息(10派X元)</th><th>进度</th><th>除权除息日</th><th>股权登记日</th></tr>
        <tr><td>2026-09-01</td><td>0</td><td>0</td><td>2</td><td>实施</td>
        <td>2026-09-11</td><td>2026-09-10</td></tr></table>'''
        page = collector.parse_page(html, code='600001', source_url='https://example.test/600001')
        self.assertEqual(page['status'], 'ok')
        self.assertEqual(page['source_url'], 'https://example.test/600001')
        self.assertEqual(page['parse_version'], collector.PARSE_VERSION)
        self.assertFalse(page['all_corporate_actions'])
        self.assertEqual(page['coverage']['scope'], 'share_bonus_table')
        empty = collector.parse_page('<table><tr><th>送股</th><th>转增</th><th>派息</th><th>进度</th><th>除权除息日</th><th>股权登记日</th></tr></table>', code='600001', source_url='u')
        self.assertEqual(empty['status'], 'empty_share_bonus_table')
        self.assertFalse(empty['verified'])

    def test_apply_dry_run_recomputes_event_without_writing(self):
        spec = importlib.util.spec_from_file_location(
            'apply_corp_actions', Path(__file__).parents[1] / 'scripts' / 'apply_corp_actions.py')
        apply = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(apply)
        with tempfile.TemporaryDirectory() as temp:
            db = Store(str(Path(temp) / 'test.db'))
            with db._conn() as conn:
                conn.executemany(
                    'INSERT INTO stock_kline_daily(trade_date, stock_code, close, prev_close, source) VALUES(?,?,?,?,?)',
                    [(DAY0, '600001', 10, 99, 'tencent_old'),
                     (DAY1, '600001', 9.8, 88, 'tencent_old')])
            db.kv_set('corp_actions:600001', {'verified': True, 'complete': True,
                                              'events': [event(kind='cash', cash_ps=.2,
                                                               bonus_ps=0, convert_ps=0)]})
            with patch.object(apply, 'store', db):
                report = apply.main(['--code', '600001', '--dry-run', '--report-out',
                                     str(Path(temp) / 'report.json')])
            self.assertEqual(report['updated'], 0)
            self.assertEqual(report['factors'], 1)
            with db._conn() as conn:
                row = conn.execute('SELECT prev_close, tr_factor, input_meta FROM stock_kline_daily WHERE trade_date=?', (DAY1,)).fetchone()
            self.assertEqual(row['prev_close'], 88)
            self.assertIsNone(row['tr_factor'])
            self.assertIsNone(row['input_meta'])
            with patch.object(apply, 'store', db):
                apply.main(['--code', '600001', '--report-out', ''])
            with db._conn() as conn:
                row = conn.execute('SELECT prev_close, tr_factor FROM stock_kline_daily WHERE trade_date=?', (DAY1,)).fetchone()
            self.assertEqual(row['prev_close'], 88)
            self.assertAlmostEqual(row['tr_factor'], 1.0)



if __name__ == '__main__':
    unittest.main()
