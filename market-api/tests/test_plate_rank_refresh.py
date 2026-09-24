import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app.core.store import Store
from app.services import plate_rank_refresh


TZ = ZoneInfo("Asia/Shanghai")


class PlateRankRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / "db.sqlite"))
        self.patcher = patch.object(plate_rank_refresh, "store", self.db)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def _rows(self, score=100):
        return [
            {"plate_code": "801045", "plate_name": "医药", "score": score, "rate": 1.2, "money_leader": 1e8},
            {"plate_code": "801001", "plate_name": "芯片", "score": score - 10, "rate": 0.5, "money_leader": 2e8},
        ] + [
            {"plate_code": f"801{i:03d}", "plate_name": f"题材{i}", "score": i, "rate": 0.1, "money_leader": 1.0}
            for i in range(100, 200)
        ]

    def test_fingerprint_uses_top_scores(self):
        fp = plate_rank_refresh.fingerprint(self._rows(12646))
        self.assertTrue(fp.startswith("801045:12646|801001:12636"))

    def test_intraday_skips_when_final(self):
        self.db.plate_rank_save(17, "2026-09-21", self._rows())
        self.db.plate_rank_day_meta_save(
            17, "2026-09-21", status="final", source="x", collected_at="2026-09-21T16:00:00+08:00",
            row_count=102, fingerprint="a",
        )
        with patch.object(plate_rank_refresh.kaipanla_plate, "fetch_realtime_rank", new=AsyncMock()) as fetch:
            out = asyncio.run(plate_rank_refresh.refresh_intraday("2026-09-21"))
        fetch.assert_not_called()
        self.assertEqual(out["skipped"], "final")

    def test_finalize_marks_final_when_fingerprint_stable(self):
        rows = self._rows(500)
        self.db.plate_rank_save(17, "2026-09-21", rows)
        fp = plate_rank_refresh.fingerprint(rows)
        self.db.plate_rank_day_meta_save(
            17, "2026-09-21", status="partial_preview", source="x",
            collected_at="2026-09-21T15:35:00+08:00", row_count=len(rows), fingerprint=fp,
        )
        with patch.object(plate_rank_refresh.kaipanla_plate, "fetch_realtime_rank", new=AsyncMock(return_value=rows)):
            with patch.object(plate_rank_refresh.kaipanla_plate, "_publish", new=AsyncMock(return_value={
                "date": "2026-09-21", "rows": len(rows), "exact": len(rows),
                "source": "kaipanla_zhishu_ranking", "collected_at": "2026-09-21T15:40:00+08:00",
            })) as pub:
                out = asyncio.run(plate_rank_refresh.finalize_today(
                    "2026-09-21", now=datetime(2026, 9, 21, 15, 40, tzinfo=TZ),
                ))
        pub.assert_awaited()
        self.assertEqual(out["status"], "final")
        self.assertEqual(out["finalize_reason"], "stable")
        self.assertEqual(self.db.plate_rank_day_meta_get(17, "2026-09-21")["status"], "final")

    def test_day_status_marks_stale_preview(self):
        self.db.plate_rank_day_meta_save(
            17, "2026-09-21", status="partial_preview", source="x",
            collected_at="2026-09-21T10:00:00+08:00", row_count=270, fingerprint="a",
        )
        status = plate_rank_refresh.day_status(
            "2026-09-21", now=datetime(2026, 9, 21, 10, 20, tzinfo=TZ),
        )
        self.assertEqual(status, "stale")

    def test_past_complete_preview_reads_as_final(self):
        """过去日卡在 partial_preview 时，3/5 日窗口仍应能求和。"""
        self.db.plate_rank_day_meta_save(
            17, "2026-09-22", status="partial_preview", source="x",
            collected_at="2026-09-22T23:59:00+08:00", row_count=259, fingerprint="a",
        )
        now = datetime(2026, 9, 24, 17, 0, tzinfo=TZ)
        self.assertTrue(plate_rank_refresh.is_final("2026-09-22", now=now))
        self.assertEqual(plate_rank_refresh.day_status("2026-09-22", now=now), "final")
        # 当天预览即使行数够，也不视同定稿
        self.db.plate_rank_day_meta_save(
            17, "2026-09-24", status="partial_preview", source="x",
            collected_at="2026-09-24T10:00:00+08:00", row_count=259, fingerprint="b",
        )
        self.assertFalse(plate_rank_refresh.is_final("2026-09-24", now=now))

    def test_history_gaps_skip_final_complete_days(self):
        self.db.plate_rank_save(17, "2026-09-18", self._rows())
        self.db.plate_rank_day_meta_save(
            17, "2026-09-18", status="final", source="x", collected_at="2026-09-18T16:00:00+08:00",
            row_count=102, fingerprint="a",
        )
        self.db.kv_set("collector_calendar_v1", {"days": ["2026-09-17", "2026-09-18", "2026-09-21"]})
        gaps = plate_rank_refresh.history_gaps("2026-09-21", lookback=5)
        self.assertIn("2026-09-17", gaps)
        self.assertNotIn("2026-09-18", gaps)
        self.assertNotIn("2026-09-21", gaps)


if __name__ == "__main__":
    unittest.main()
