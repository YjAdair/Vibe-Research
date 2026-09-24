import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.core.store import Store
from app.services import daily_close
from app.datasources import tencent
from app.datasources import eastmoney


class DailyCloseOhlcTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / "db.sqlite"))
        self.db.daily_close_save("2026-09-09", [
            {"stock_code": "000001", "stock_name": "平安银行", "market_type": "深", "concept": "银行", "close": 10.0, "prev_close": 9.1},
        ], {"complete": True, "trade_date": "2026-09-09"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_patch_fills_missing_ohlc_without_replacing_close(self):
        n = self.db.daily_close_patch_ohlc("2026-09-09", [
            {"stock_code": "000001", "open": 9.5, "high": 10.2, "low": 9.4, "close": 99},
        ])
        row = self.db.daily_close_range("2026-09-09", 1)[0]
        self.assertEqual(n, 1)
        self.assertEqual(row["close"], 10.0)
        self.assertEqual(row["open"], 9.5)
        self.assertEqual(row["high"], 10.2)
        self.assertEqual(row["low"], 9.4)
        n2 = self.db.daily_close_patch_ohlc("2026-09-09", [
            {"stock_code": "000001", "open": 1, "high": 2, "low": 3},
        ])
        row = self.db.daily_close_range("2026-09-09", 1)[0]
        self.assertEqual(n2, 0)
        self.assertEqual(row["open"], 9.5)

    async def test_patch_ohlc_reads_tencent_bars_and_does_not_resave_snapshot(self):
        async def fake_kline(symbol, n=80):
            return {"x": ["20260909"], "y": [[9.5, 10.0, 10.2, 9.4, 10.0]], "vol": [1], "code": "000001"}
        with patch.object(daily_close, "store", self.db), patch.object(tencent, "kline_day_by_symbol", AsyncMock(side_effect=fake_kline)):
            out = await daily_close.patch_ohlc(["2026-09-09"], sample_codes=["000001"])
        row = self.db.daily_close_range("2026-09-09", 1)[0]
        self.assertEqual(out["patched"]["2026-09-09"], 1)
        self.assertEqual(row["close"], 10.0)
        self.assertEqual(row["open"], 9.5)

    def test_patch_turnover_fills_missing_without_replacing(self):
        n = self.db.daily_close_patch_turnover("2026-09-09", [
            {"stock_code": "000001", "turnover_ratio": 0.5, "circulation_value": 2.0e11},
        ])
        row = self.db.daily_close_range("2026-09-09", 1)[0]
        self.assertEqual(n, 1)
        self.assertEqual(row["turnover_ratio"], 0.5)
        self.assertEqual(row["circulation_value"], 2.0e11)
        n2 = self.db.daily_close_patch_turnover("2026-09-09", [
            {"stock_code": "000001", "turnover_ratio": 9.9, "circulation_value": 1.0e9},
        ])
        row = self.db.daily_close_range("2026-09-09", 1)[0]
        self.assertEqual(n2, 0)
        self.assertEqual(row["turnover_ratio"], 0.5)
        self.assertEqual(row["circulation_value"], 2.0e11)

    async def test_patch_turnover_computes_vol_ratio_from_prev5_avg(self):
        self.db.daily_close_save("2026-09-10", [
            {"stock_code": "000001", "stock_name": "平安银行", "market_type": "深", "concept": "银行", "close": 10.0, "prev_close": 9.1},
        ], {"complete": True, "trade_date": "2026-09-10"})
        dates = ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
                 "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10"]
        bars = [
            {"date": d, "open": 10, "close": 10, "high": 11, "low": 9,
             "volume": 100 if d != "2026-09-10" else 250,
             "amount": 1e8, "amplitude": 1, "pct_chg": 1, "change": 0.1, "turnover_rate": 5.0}
            for d in dates
        ]
        with patch.object(daily_close, "store", self.db), patch.object(eastmoney, "kline_history", AsyncMock(return_value=bars)), \
                patch.object(tencent, "realtime", AsyncMock(return_value={})):
            out = await daily_close.patch_turnover(dates=["2026-09-10"], codes=["000001"])
        row = next(r for r in self.db.daily_close_range("2026-09-10", 1) if r["trade_date"] == "2026-09-10")
        self.assertEqual(out["total"], 1)
        self.assertEqual(row["turnover_ratio"], 5.0)
        self.assertEqual(row["vol_ratio"], 2.5)
