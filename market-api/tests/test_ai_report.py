import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.core.store import Store
from app.services import ai_report, pools, popular, sentiment, topic, jyjy
from app.services import quant


class AiReportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / "db.sqlite"))
        self.mode = settings.collector_mode
        settings.collector_mode = "external"
        self.db.limit_pool_save("em_up", "2026-09-10", {
            "pool_kind": "up", "source": "eastmoney", "date": "2026-09-10", "requested_date": "2026-09-10",
            "total": 1, "complete": True, "empty_ok": True,
            "pool": [{"c": "000001", "n": "平安银行", "zdp": 10}],
            "concepts_by_code": {"000001": ["银行"]},
        })
        self.db.limit_pool_save("em_down", "2026-09-10", {
            "pool_kind": "down", "source": "eastmoney", "date": "2026-09-10", "requested_date": "2026-09-10",
            "total": 0, "complete": True, "empty_ok": True, "pool": [],
        })
        self.db.limit_pool_save("em_broken", "2026-09-10", {
            "pool_kind": "broken", "source": "eastmoney", "date": "2026-09-10", "requested_date": "2026-09-10",
            "total": 0, "complete": True, "empty_ok": True, "pool": [],
        })
        self.db.topic_snapshot_save("2026-09-10", [{
            "unique_key": "BK1", "name": "绿色电力", "score": 90, "today_pct": 2.1, "limit_up_count": 1,
            "stocks": [{"code": "000001", "name": "平安银行", "pct": 10}],
        }])
        items = [{"symbol_code": f"{i:06d}", "symbol_name": "N"+str(i), "rank": i, "rank_diff": 0} for i in range(1, 101)]
        items[0] = {"symbol_code": "000001", "symbol_name": "平安银行", "rank": 1, "rank_diff": 0}
        self.db.popular_save("2026-09-10", {"date": "2026-09-10", "complete": True, "total": 100, "items": items, "source": "x"})
        self.db.daily_close_save("2026-09-10", [
            {"stock_code": "000001", "stock_name": "平安银行", "market_type": "深", "concept": "银行", "close": 11, "prev_close": 10, "open": 10.5, "high": 11.2, "low": 10.4},
        ], {"complete": True, "trade_date": "2026-09-10"})

    def tearDown(self):
        settings.collector_mode = self.mode
        self.tmp.cleanup()

    async def test_builds_from_published_snapshots_without_upstream(self):
        with patch.object(ai_report, "store", self.db), patch.object(pools, "store", self.db), patch.object(popular, "store", self.db), patch.object(jyjy, "store", self.db), patch.object(quant, "store", self.db), patch.object(sentiment, "sentiment_today", AsyncMock(return_value={"index": 47, "info": [{"lbgd": 4}]})):
            item = await ai_report.build("morning", "2026-09-10")
            self.assertEqual(item["id"], "morning:2026-09-10")
            self.assertNotIn("绿色电力", item["content"])
            self.assertEqual(item["status"], "missing_input")
            self.assertNotIn("平安银行", item["content"])
            listed = await ai_report.list_reports("morning")
            self.assertEqual(listed["total"], 1)
            detail = await ai_report.detail("morning:2026-09-10")
            self.assertEqual(detail["title"], item["title"])

    async def test_api_list_and_detail(self):
        from app.main import app
        import httpx
        with patch("app.core.store.store", self.db), patch.object(ai_report, "store", self.db), patch.object(pools, "store", self.db), patch.object(popular, "store", self.db), patch.object(jyjy, "store", self.db), patch.object(quant, "store", self.db), patch.object(sentiment, "sentiment_today", AsyncMock(return_value={"index": 47, "info": [{"lbgd": 4}]})):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                listed = (await client.get("/v3/ai-report/list?type=morning")).json()
                detail = (await client.get("/v3/ai-report/detail/morning:2026-09-10")).json()
                cfg = (await client.get("/v3/topic/monitor/panorama/config?date=2026-09-10")).json()
        self.assertEqual(listed["code"], 20000)
        self.assertEqual(listed["data"]["items"], [])
        self.assertEqual(detail["data"]["status"], "missing")
        self.assertEqual(cfg["data"]["v2_position_fractions"], [0.25, 0.25])
