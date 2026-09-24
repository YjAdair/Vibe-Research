import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.core.store import Store
from app.services import ai_report, pools, popular


class HotspotReportSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / "db.sqlite"))
        self.db.kv_set("collector_calendar_v1", {"complete": True, "days": ["2026-09-10", "2026-09-11"]})

    def tearDown(self):
        self.tmp.cleanup()

    def _close(self, day, available_at=None):
        self.db.daily_close_save(
            day,
            [{"stock_code": "000001", "stock_name": "样本", "close": 11, "prev_close": 10}],
            {"complete": True, "trade_date": day, "source": "close", "source_as_of": f"{day}T15:00:00+08:00", "available_at": available_at or f"{day}T15:00:00+08:00"},
        )

    def _pool(self, kind, day, count=1, available_at=None):
        rows = [{"c": f"00000{i + 1}", "n": "样本"} for i in range(count)]
        self.db.limit_pool_save(f"em_{kind}", day, {
            "pool_kind": kind, "source": "pool", "date": day, "total": count, "pool": rows,
            "complete": True, "source_as_of": f"{day}T15:00:00+08:00", "available_at": available_at or f"{day}T15:00:00+08:00",
        })

    def _topic_and_popular(self, day, available_at=None):
        stamp = available_at or f"{day}T15:00:00+08:00"
        self.db.topic_snapshot_save(day, [{
            "formula_version": "ml_r1_v1_1", "input_snapshot_id": "test", "status": "final",
            "unique_key": "BK1", "name": "旧日题材", "score": 1, "today_pct": 1,
            "limit_up_count": 1, "available_at": stamp, "trade_date":day, "complete":True, "source":"eastmoney",
        }])
        self.db.popular_save(day, {
            "date": day, "complete": True, "total": 1,
            "items": [{"symbol_code": "000001", "symbol_name": "样本", "rank": 1}], "source": "10jqka_eq_hot_list",
            "available_at": stamp, "source_as_of":f"{day}T15:00:00+08:00",
        })

    async def test_morning_report_uses_previous_completed_day(self):
        self._close("2026-09-10")
        self._pool("up", "2026-09-10", 1)
        self._pool("down", "2026-09-10", 0)
        self._pool("broken", "2026-09-10", 0)
        self._topic_and_popular("2026-09-10")
        self._close("2026-09-11", available_at="2026-09-11T15:00:00+08:00")
        self._pool("up", "2026-09-11", 9, "2026-09-11T15:00:00+08:00")

        with patch.object(ai_report, "store", self.db), patch.object(pools, "store", self.db), patch.object(popular, "store", self.db):
            report = await ai_report.build("morning", "2026-09-11")

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["data_as_of"], "2026-09-10")
        self.assertEqual(report["ztjs"], 1)
        self.assertNotIn("涨停 9 家", report["content"])
        self.assertEqual(report["cutoff"], "2026-09-11T09:30:00+08:00")

    async def test_late_collection_is_rejected_for_morning_but_valid_after_close(self):
        day = "2026-09-10"
        self._close(day, "2026-09-11T10:00:00+08:00")
        for kind in ('up', 'down', 'broken'):
            self._pool(kind, day, 0, day + 'T15:20:00+08:00')
        self._topic_and_popular(day, day + 'T15:20:00+08:00')
        with patch.object(ai_report, 'store', self.db), patch.object(pools, 'store', self.db), patch.object(popular, 'store', self.db):
            morning = await ai_report.build('morning', '2026-09-11')
            self.assertEqual(morning['status'], 'missing_input')
            self._close(day, day + 'T15:20:00+08:00')
            evening = await ai_report.build('evening', day)
            self.assertEqual(evening['status'], 'ok')
            self.assertEqual(evening['ztjs'], 0)
        malformed = {'date':day, 'source':'test','complete':True,'total':0,
                     'available_at': day+'T15:20:00+08:00', 'source_as_of':day+'T15:00:00+08:00'}
        self.assertIn('incomplete_rows', ai_report._validate_record('up', malformed, day, ai_report._parse_at(day+'T20:00:00+08:00'))[1])

    async def test_list_and_detail_are_read_only(self):
        with patch.object(ai_report, "store", self.db), patch.object(ai_report, "build", AsyncMock(side_effect=AssertionError("query must not build"))) as build:
            listed = await ai_report.list_reports("morning")
            detail = await ai_report.detail("morning:2026-09-11")

        build.assert_not_awaited()
        self.assertEqual(listed["status"], "missing")
        self.assertEqual(detail["status"], "missing")
        self.assertIsNone(self.db.ai_report_get("morning:2026-09-11"))

    async def test_missing_pool_is_missing_input_and_not_zero(self):
        day = "2026-09-10"
        self._close(day)
        self._pool("up", day, 1)
        self._topic_and_popular(day)

        with patch.object(ai_report, "store", self.db), patch.object(pools, "store", self.db), patch.object(popular, "store", self.db):
            report = await ai_report.build("evening", day)

        self.assertEqual(report["status"], "missing_input")
        self.assertIsNone(report["df_num"])
        self.assertIn("跌停 — 家", report["content"])
        self.assertNotIn("跌停 0 家", report["content"])


if __name__ == "__main__":
    unittest.main()
