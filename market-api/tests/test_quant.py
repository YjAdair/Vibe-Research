import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.core.store import Store
from app.services import quant, pools


class StrategyBacktestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / "db.sqlite"))
        self.mode = settings.collector_mode
        settings.collector_mode = "external"
        self.db.limit_pool_save("em_up", "2026-09-09", {
            "pool_kind": "up", "source": "eastmoney", "date": "2026-09-09", "requested_date": "2026-09-09",
            "total": 1, "complete": True, "empty_ok": True,
            "pool": [{"c": "000001", "n": "平安银行", "zdp": 10}],
            "concepts_by_code": {"000001": ["银行"]},
        })
        self.db.daily_close_save("2026-09-09", [
            {"stock_code": "000001", "stock_name": "平安银行", "market_type": "深", "concept": "银行", "close": 10.0, "prev_close": 9.1},
        ], {"complete": True, "trade_date": "2026-09-09"})
        self.db.daily_close_save("2026-09-10", [
            {"stock_code": "000001", "stock_name": "平安银行", "market_type": "深", "concept": "银行", "close": 11.0, "prev_close": 10.0, "open": 10.5, "high": 11.2, "low": 10.4},
        ], {"complete": True, "trade_date": "2026-09-10"})

    def tearDown(self):
        settings.collector_mode = self.mode
        self.tmp.cleanup()

    async def test_uses_next_open_not_close_and_skips_missing_open(self):
        with patch.object(quant, "store", self.db), patch.object(pools, "store", self.db):
            data = quant.backtest(window=5, cap_per_day=8)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["total_trades"], 1)
        rec = data["daily"]["2026-09-10"]["records"][0]
        self.assertEqual(rec["trade_price"], 10.5)
        self.assertEqual(rec["sell_price"], 11.0)
        self.assertGreater(data["total_return_pct"], 0)

    async def test_api_equity_is_dated_rows_not_synthetic_curve(self):
        from app.main import app
        import httpx
        with patch("app.core.store.store", self.db), patch.object(quant, "store", self.db), patch.object(pools, "store", self.db):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                perf = (await client.get("/v3/quant/strategy/performance")).json()
                eq = (await client.get("/v3/quant/strategy/equity")).json()
                detail = (await client.get("/v3/quant/strategy/daily-details?date=2026-09-10")).json()
                missing = (await client.get("/v3/quant/strategy/daily-details?date=2026-09-01")).json()
        self.assertEqual(perf["data"]["status"], "ok")
        self.assertEqual(eq["data"][0]["date"], "2026-09-10")
        self.assertIn("equity", eq["data"][0])
        self.assertEqual(detail["data"]["records"][0]["symbol_code"], "000001")
        self.assertIn(missing["data"]["status"], ("missing", "skipped_no_open"))
