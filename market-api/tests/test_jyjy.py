import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.core.store import Store
from app.services import jyjy, popular


class JyJyPoolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / "db.sqlite"))
        self.mode = settings.collector_mode
        settings.collector_mode = "external"
        rows_prev = [
            {"stock_code": "002084", "stock_name": "海鸥住工", "market_type": "深", "concept": "智能家居", "close": 6.12, "prev_close": 6.8},
            {"stock_code": "600000", "stock_name": "浦发银行", "market_type": "沪", "concept": "银行", "close": 10.2, "prev_close": 10.1},
            {"stock_code": "300001", "stock_name": "特锐德", "market_type": "深", "concept": "充电桩", "close": 20.0, "prev_close": 22.0},
        ]
        rows_today = [
            {"stock_code": "002084", "stock_name": "海鸥住工", "market_type": "深", "concept": "智能家居", "close": 5.52, "prev_close": 6.12, "open": 5.9, "high": 5.99, "low": 5.52},
            {"stock_code": "600000", "stock_name": "浦发银行", "market_type": "沪", "concept": "银行", "close": 10.3, "prev_close": 10.2, "open": 10.2, "high": 10.4, "low": 10.1},
            {"stock_code": "300001", "stock_name": "特锐德", "market_type": "深", "concept": "充电桩", "close": 19.5, "prev_close": 20.0, "open": 19.8, "high": 20.1, "low": 19.4},
        ]
        self.db.daily_close_save("2026-09-09", rows_prev, {"complete": True, "trade_date": "2026-09-09"})
        self.db.daily_close_save("2026-09-10", rows_today, {"complete": True, "trade_date": "2026-09-10"})
        items = [{"symbol_code": f"{i:06d}", "symbol_name": "N"+str(i), "rank": i, "rank_diff": 0} for i in range(1, 101)]
        items[0] = {"symbol_code": "002084", "symbol_name": "海鸥住工", "rank": 1, "rank_diff": 0, "popularity_tag": "首板"}
        self.db.popular_save("2026-09-10", {"date": "2026-09-10", "complete": True, "total": 100, "items": items, "source": "10jqka_eq_hot_list"})

    def tearDown(self):
        settings.collector_mode = self.mode
        self.tmp.cleanup()

    async def test_9008_uses_published_close_and_keeps_oversold(self):
        with patch.object(jyjy, "store", self.db), patch.object(popular, "store", self.db):
            data = await jyjy.pool(9008, "2026-09-10")
        codes = [i["symbol_code"] for i in data["items"]]
        self.assertIn("002084", codes)
        self.assertNotIn("600000", codes)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["items"][0]["pct_pre1"], -0.1)

    async def test_9016_is_mainboard_only(self):
        with patch.object(jyjy, "store", self.db), patch.object(popular, "store", self.db):
            data = await jyjy.pool(9016, "2026-09-10")
        codes = [i["symbol_code"] for i in data["items"]]
        self.assertIn("600000", codes)
        self.assertIn("002084", codes)
        self.assertNotIn("300001", codes)

    async def test_9009_intersects_hot_list(self):
        with patch.object(jyjy, "store", self.db), patch.object(popular, "store", self.db):
            data = await jyjy.pool(9009, "2026-09-10")
        self.assertEqual([i["symbol_code"] for i in data["items"]], ["002084"])

    async def test_missing_date_does_not_fallback(self):
        with patch.object(jyjy, "store", self.db), patch.object(popular, "store", self.db):
            data = await jyjy.pool(9008, "2026-09-01")
        self.assertEqual(data["status"], "missing")
        self.assertEqual(data["items"], [])

    async def test_api_shape(self):
        from app.main import app
        import httpx
        with patch("app.core.store.store", self.db), patch.object(jyjy, "store", self.db), patch.object(popular, "store", self.db):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                body = (await client.get("/v3/api/quant/stock/pool/jyzj/9008?date=2026-09-10")).json()
                compat = (await client.get("/v3/quant/stock/pool/jyzj/9008?date=2026-09-10")).json()
        self.assertEqual(body["code"], 20000)
        self.assertEqual(body["data"]["items"][0]["symbol_code"], "002084")
        self.assertEqual(compat["data"]["quant_code"], 9008)
