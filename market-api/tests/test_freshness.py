"""数据发布新鲜度监控测试。"""
import sys
import os
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core import freshness  # noqa: E402

TZ = ZoneInfo('Asia/Shanghai')


def _mk_marker(table, latest_day, minute=None):
    """返回可替换 _latest_marker 的 stub。"""
    def _stub(db, rule):
        if rule.table != table:
            return ('2099-01-01', '')
        return (latest_day, minute or '')
    return _stub


class TestFreshness(unittest.TestCase):
    def test_mixed_legacy_date_formats_cannot_hide_newer_published_day(self):
        import tempfile
        from pathlib import Path
        from app.core.store import Store
        with tempfile.TemporaryDirectory() as tmp:
            db = Store(str(Path(tmp) / 'freshness.db'))
            with db._conn() as conn:
                conn.executemany('INSERT INTO daily_close_runs VALUES(?,?)', [('20260911', '{}'), ('2026-09-15', '{}')])
            rule = next(r for r in freshness.RULES if r.name == 'daily_close')
            self.assertEqual(freshness._latest_marker(db, rule), ('2026-09-15', ''))
            for day in ('20260911', '2026-09-11', '2026-09-15'):
                db.daily_close_save(day, [{'stock_code':'000001','close':10}], {'complete':True})
            self.assertEqual(db.daily_close_dates(2), ['2026-09-15', '2026-09-11'])

    def _eval(self, marker_stub, now, cal_days=('2026-09-11',)):
        from unittest import mock
        with mock.patch.object(freshness, '_latest_marker', marker_stub), \
             mock.patch.object(freshness.store, 'kv_get', return_value={'days': list(cal_days)}):
            return freshness.evaluate(now=now)

    def test_not_in_session(self):
        r = self._eval(lambda db, rule: None, datetime(2026, 9, 11, 3, 0, tzinfo=TZ))
        self.assertEqual(r['session'], 'not_in_session')

    def test_weekend_market_clock_is_not_a_session(self):
        # 周日即使处于 09:15-15:10 的时钟窗口，也不能触发新鲜度告警。
        r = self._eval(lambda db, rule: None, datetime(2026, 9, 13, 10, 0, tzinfo=TZ))
        self.assertEqual(r['session'], 'not_in_session')
        self.assertEqual(r['domains'], {})

    def test_intraday_stale_when_not_today(self):
        # 盘中 10:00，popular 最新还是昨天 -> stale
        stub = _mk_marker('popular_snapshots', '2026-09-10')
        r = self._eval(stub, datetime(2026, 9, 11, 10, 0, tzinfo=TZ))
        self.assertEqual(r['domains']['popular']['status'], 'stale')

    def test_intraday_ok_when_today(self):
        stub = _mk_marker('popular_snapshots', '2026-09-11')
        r = self._eval(stub, datetime(2026, 9, 11, 10, 0, tzinfo=TZ))
        self.assertEqual(r['domains']['popular']['status'], 'ok')

    def test_minute_age_stale(self):
        # 盘中 10:30，minute_samples 最新 10:00 -> age 30 分钟 > 10 容忍度 -> stale
        stub = _mk_marker('minute_samples', '2026-09-11', '10:00:07')
        r = self._eval(stub, datetime(2026, 9, 11, 10, 30, tzinfo=TZ))
        self.assertEqual(r['domains']['market_minute']['status'], 'stale')

    def test_close_pending_before_deadline(self):
        # 15:20 daily_close 还没出，deadline 15:40 -> pending 不告警
        stub = _mk_marker('daily_close_runs', '2026-09-10')
        r = self._eval(stub, datetime(2026, 9, 11, 15, 20, tzinfo=TZ))
        self.assertEqual(r['domains']['daily_close']['status'], 'pending')

    def test_close_stale_after_deadline(self):
        # 15:50 daily_close 仍是昨天 -> stale
        stub = _mk_marker('daily_close_runs', '2026-09-10')
        r = self._eval(stub, datetime(2026, 9, 11, 15, 50, tzinfo=TZ))
        self.assertEqual(r['domains']['daily_close']['status'], 'stale')

    def test_calendar_unconfirmed_no_false_stale(self):
        # 日历为空（未确认）：盘中 stale 应降级为 pending
        stub = _mk_marker('popular_snapshots', '2026-09-10')
        r = self._eval(stub, datetime(2026, 9, 11, 10, 0, tzinfo=TZ), cal_days=())
        self.assertEqual(r['domains']['popular']['status'], 'pending')
        self.assertFalse(r['calendar_ok'])

    def test_after_close_intraday_domains_ok(self):
        # 收盘后 16:00，盘中域只要今天有数据就 ok
        stub = _mk_marker('popular_snapshots', '2026-09-11')
        r = self._eval(stub, datetime(2026, 9, 11, 16, 0, tzinfo=TZ))
        self.assertEqual(r['domains']['popular']['status'], 'ok')


if __name__ == "__main__":
    unittest.main()
