import tempfile
import unittest
import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.core.store import Store
from app.services import free_hotspots


class FreeHotspotsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / "db.sqlite"))

    def tearDown(self):
        self.tmp.cleanup()

    async def test_ml_modes_freeze_members_before_open_and_never_refill_from_future(self):
        for hour, stocks in (("09:00", ["000001"]), ("15:01", ["000002"])):
            self.db.kv_append_snapshot("free_hotspots:members:BK1", {
                "stocks": stocks, "as_of": "2026-09-10", "status": "ok", "source": "eastmoney",
                "collected_at": "2026-09-10T" + hour + ":00+08:00"})
        self.db.popular_save("2026-09-10", {"date": "2026-09-10", "complete": True, "source": "ths", "total": 2,
            "items": [{"symbol_code": "000002", "rank": 1}, {"symbol_code": "000001", "rank": 2}]})
        with patch.object(free_hotspots, "store", self.db), patch.object(free_hotspots, "members_snapshot", AsyncMock()) as fetch:
            self.assertEqual(free_hotspots.membership("BK1", "2026-09-10", at_open=True)["stocks"], ["000001"])
            self.assertEqual(free_hotspots.membership("BK1", "2026-09-10")["stocks"], ["000002"])
            rates = await free_hotspots.stock_rates("BK1", "2026-09-10", 1, 30, at_open=True)
            popular = await free_hotspots.popular_rank("BK1", "2026-09-10", 1, 30, at_open=True)
            missing = await free_hotspots.stock_rates("BK1", "2026-09-09", 1, 30, at_open=True)
            self.assertEqual([r["stock_code"] for r in rates["list"]], ["000001"])
            self.assertEqual([r["stock_code"] for r in popular["list"]], ["000001"])
            self.assertEqual(missing["meta"]["status"], "missing_preopen_members")
            fetch.assert_not_awaited()

    def calendar(self):
        self.db.kv_set("collector_calendar_v1", {"complete": True, "days": ["2026-09-08", "2026-09-09", "2026-09-10"]})

    async def test_rank_days_uses_native_concept_and_merges_snapshot_without_double_count(self):
        self.calendar()
        self.db.board_history_save(3, "BK1", [
            {"date1": "2026-09-09", "plate_code": "BK1", "plate_name": "概念一", "pct": 2, "amount": 100, "main_net_inflow": 10},
            {"date1": "2026-09-10", "plate_code": "BK1", "plate_name": "概念一", "pct": 3, "amount": 110, "main_net_inflow": 20},
        ])
        self.db.board_snapshot_save(3, "2026-09-10", [{"date1": "2026-09-10", "plate_code": "BK1", "plate_name": "概念一", "pct": 4, "main_net_inflow": 22}], {"trade_date": "2026-09-10", "source_as_of": "2026-09-10T15:00:00+08:00", "collected_at": "2026-09-10T15:01:00+08:00"})
        with patch.object(free_hotspots, "store", self.db):
            rows = await free_hotspots.rank_days("20260910", 2, 1, plate_type=15)
        self.assertEqual(rows[0]["plate_code"], "BK1")
        self.assertEqual(rows[0]["sum_rate"], 6)
        self.assertEqual(rows[0]["days"], 2)
        self.assertEqual(rows[0]["taxonomy"], "eastmoney_concept")
        self.assertEqual(rows[0]["sum_score"], None)
        self.assertEqual(rows[0]["formula_note"], "sum_daily_pct_non_compounded")
        self.assertEqual(rows[0]["source_as_of"], "2026-09-10T15:00:00+08:00")
        self.assertEqual(rows[0]["collected_at"], "2026-09-10T15:01:00+08:00")
        self.assertEqual(rows[0]["data_kind"], "observed_snapshot")

    async def test_rank_days_missing_metric_is_null_and_type9_is_empty(self):
        self.calendar()
        self.db.board_history_save(2, "BK2", [
            {"date1": "2026-09-09", "plate_code": "BK2", "plate_name": "行业二", "pct": 1, "main_net_inflow": None},
            {"date1": "2026-09-10", "plate_code": "BK2", "plate_name": "行业二", "pct": 2, "main_net_inflow": None},
        ])
        with patch.object(free_hotspots, "store", self.db):
            flow = await free_hotspots.rank_days("2026-09-10", 2, 3, plate_type=14)
            rate = await free_hotspots.rank_days("2026-09-10", 2, 1, plate_type=14)
            unsupported = await free_hotspots.rank_days("2026-09-10", 2, 9, plate_type=17)
        self.assertEqual(flow, [])
        self.assertEqual(rate[0]["sum_rate"], 3)
        self.assertIsNone(rate[0]["sum_leader_money"])
        self.assertEqual(rate[0]["data_status"], "partial")
        self.assertEqual(unsupported["status"], "unsupported_taxonomy")
        self.assertEqual(unsupported["target_taxonomy_id"], 17)

    async def test_target_taxonomies_never_use_eastmoney_board_history(self):
        self.calendar()
        self.db.board_history_save(3, "BK1", [
            {"date1": "2026-09-10", "plate_code": "BK1", "plate_name": "概念一", "pct": 4}
        ])
        with patch.object(free_hotspots, "store", self.db):
            for taxonomy_id in (17, 18):
                payload = await free_hotspots.rank_days("2026-09-10", 1, 1, plate_type=taxonomy_id)
                self.assertEqual(payload["status"], "unsupported_taxonomy")
                self.assertEqual(payload["data"], [])
                self.assertIsNone(payload["source"])

    async def test_members_are_actual_collect_date_and_replay_only_not_later(self):
        data = {"total": 2, "source": "eastmoney", "source_as_of": "20260910T150000", "stocks": [
            {"code": "000001", "name": "A", "pct": 1, "price": 10, "amount": 100, "source_timestamp": 123},
            {"code": "600001", "name": "B", "pct": -1, "price": 9, "amount": 90, "source_timestamp": 123},
        ]}
        with patch.object(free_hotspots, "store", self.db), patch.object(free_hotspots.boards, "members", AsyncMock(return_value=data)), patch.object(free_hotspots, "datetime") as dt:
            dt.now.return_value = datetime(2026, 9, 10, 15, 1, tzinfo=free_hotspots.TZ)
            got = await free_hotspots.collect_members("BK1")
            self.assertEqual(got["as_of"], "2026-09-10")
            self.assertEqual(got["stocks"], ["000001", "600001"])
            self.assertEqual(free_hotspots.membership("BK1", "2026-09-09")["status"], "missing")
            self.assertEqual(free_hotspots.membership("BK1", "2026-09-10")["as_of"], "2026-09-10")

    async def test_members_snapshot_singleflight_and_current_quote_prefers_prev_close(self):
        data = {"total": 1, "source": "eastmoney", "source_as_of": "provider-time", "stocks": [
            {"code": "000001", "name": "A", "pct": 99, "price": 11, "prev_close": 10, "amount": 100, "source_timestamp": 1789369260},
        ]}
        with patch.object(free_hotspots, "store", self.db), patch.object(settings, "collector_mode", "embedded"), patch.object(free_hotspots, "datetime", wraps=datetime) as dt, patch.object(free_hotspots.boards, "members", AsyncMock(return_value=data)) as fetch:
            dt.now.return_value = datetime(2026, 9, 14, 15, 1, tzinfo=free_hotspots.TZ)
            got = await asyncio.gather(*[free_hotspots.members_snapshot("BK-SINGLE") for _ in range(5)])
            rates = await free_hotspots.stock_rates("BK-SINGLE", "2026-09-14", 1, 20)
        self.assertEqual(fetch.await_count, 1)
        self.assertTrue(all(item["stocks"] == ["000001"] for item in got))
        self.assertAlmostEqual(rates["list"][0]["px_change_rate"], 10.0)

    async def test_external_members_snapshot_is_kv_only(self):
        with patch.object(free_hotspots, "store", self.db), patch.object(settings, "collector_mode", "external"), patch.object(free_hotspots.boards, "members", AsyncMock()) as fetch:
            result = await free_hotspots.members_snapshot("BK-NET-OFF")
        fetch.assert_not_awaited()
        self.assertEqual(result["stocks"], [])
        self.assertEqual(result["freshness"]["status"], "unknown")

    async def test_current_quote_timestamp_date_and_sixty_second_freshness_are_enforced(self):
        self.db.kv_set("free_hotspots:members:BK-STALE:2026-09-14", {
            "stocks": ["000001"], "quotes": {"000001": {"code": "000001", "name": "A", "price": 11, "prev_close": 10,
            "pct": 10, "source_timestamp": 1789365600}}, "as_of": "2026-09-14", "effective_date": "2026-09-14",
            "quotes_collected_at": "2026-09-14T15:00:00+08:00", "status": "ok", "source": "eastmoney"})
        self.db.kv_set("free_hotspots:members:BK-STALE:index", ["2026-09-14"])
        with patch.object(free_hotspots, "store", self.db), patch.object(settings, "collector_mode", "external"), patch.object(free_hotspots, "datetime", wraps=datetime) as dt, patch.object(free_hotspots.boards, "members", AsyncMock()) as fetch:
            dt.now.return_value = datetime(2026, 9, 14, 15, 2, tzinfo=free_hotspots.TZ)
            result = await free_hotspots.stock_rates("BK-STALE", "2026-09-14", 1, 20)
        fetch.assert_not_awaited()
        self.assertIsNone(result["list"][0]["px_change_rate"])
        self.assertEqual(result["list"][0]["data_status"], "stale_quote")
        self.assertEqual(result["list"][0]["freshness"]["status"], "stale")

    async def test_current_shared_market_snapshot_precedes_old_member_quote(self):
        self.db.kv_set("free_hotspots:members:BK-SHARED:2026-09-10", {
            "stocks": ["000001"], "quotes": {"000001": {"code": "000001", "name": "A", "price": 8, "prev_close": 10,
            "pct": -20, "source_timestamp": 1789020000}}, "as_of": "2026-09-10", "effective_date": "2026-09-10",
            "quotes_collected_at": "2026-09-10T15:00:00+08:00", "status": "ok", "source": "eastmoney"})
        self.db.kv_set("free_hotspots:members:BK-SHARED:index", ["2026-09-10"])
        self.db.kv_set("market_quotes_current_v1", {"date": "2026-09-14", "source_as_of": "2026-09-14T15:01:00+08:00",
            "collected_at": "2026-09-14T15:01:01+08:00", "stocks": [{"code": "000001", "name": "A", "price": 11,
            "prev_close": 10, "pct": 99, "amount": 200, "source_timestamp": 1789369260}], "complete": True})
        with patch.object(free_hotspots, "store", self.db), patch.object(settings, "collector_mode", "external"), patch.object(free_hotspots, "datetime", wraps=datetime) as dt, patch.object(free_hotspots.boards, "members", AsyncMock()) as fetch:
            dt.now.return_value = datetime(2026, 9, 14, 15, 2, tzinfo=free_hotspots.TZ)
            result = await free_hotspots.stock_rates("BK-SHARED", "2026-09-14", 1, 20)
        fetch.assert_not_awaited()
        self.assertAlmostEqual(result["list"][0]["px_change_rate"], 10.0)
        self.assertEqual(result["list"][0]["quote_source"], "market_quotes_current_v1")

    async def test_stock_rates_history_has_no_old_price_fallback_and_popular_requires_exact_date(self):
        self.db.kv_set("free_hotspots:members:BK1:2026-09-10", {"stocks": ["000001", "000002"], "quotes": {}, "as_of": "2026-09-10", "source_as_of": None, "status": "ok", "source": "eastmoney"})
        self.db.kv_set("free_hotspots:members:BK1:index", ["2026-09-10"])
        self.db.daily_close_save("2026-09-10", [{"stock_code": "000001", "stock_name": "A", "close": 11, "prev_close": 10}], {"complete": True, "trade_date": "2026-09-10"})
        self.db.popular_save("2026-09-10", {"date": "2026-09-10", "complete": True, "total": 1, "items": [{"symbol_code": "000001", "symbol_name": "A", "rank": 1}], "source": "ths"})
        with patch.object(free_hotspots, "store", self.db), patch.object(free_hotspots, "members_snapshot", AsyncMock(side_effect=AssertionError("historical request must not collect"))):
            rates = await free_hotspots.stock_rates("BK1", "2026-09-10", 1, 20)
            popular = await free_hotspots.popular_rank("BK1", "2026-09-09", 1, 20)
        self.assertEqual(rates["list"][0]["stock_code"], "000001")
        self.assertIsNone(rates["list"][0]["px_change_rate"])
        self.assertEqual(rates["list"][0]["data_status"], "missing_reference_evidence")
        self.assertIsNone(next(row for row in rates["list"] if row["stock_code"] == "000002")["px_change_rate"])
        self.assertEqual(popular["list"], [])

    async def test_pct_batch_and_kline_use_complete_prices_and_real_history(self):
        self.calendar()
        self.db.kv_set("free_hotspots:members:BK1:2026-09-10", {"stocks": ["000001"], "quotes": {}, "as_of": "2026-09-10", "status": "ok", "source": "eastmoney"})
        self.db.kv_set("free_hotspots:members:BK1:index", ["2026-09-10"])
        for d, close in [("2026-09-08", 10), ("2026-09-09", 11), ("2026-09-10", 12)]:
            self.db.daily_close_save(d, [{"stock_code": "000001", "stock_name": "A", "close": close, "prev_close": close - 1}], {"complete": True, "trade_date": d})
        self.db.board_history_save(3, "BK1", [
            {"date1": "2026-09-09", "plate_code": "BK1", "open": 9, "price": 10, "high": 11, "low": 8, "volume": 100, "amount": 1000, "turnover": 2},
            {"date1": "2026-09-10", "plate_code": "BK1", "open": 10, "price": 12, "high": 13, "low": 9, "volume": 120, "amount": 1200, "turnover": 3},
        ])
        self.db.board_snapshot_save(3, "2026-09-10", [{"date1": "2026-09-10", "plate_code": "BK1", "price": 99}], {"trade_date": "2026-09-10"})
        with patch.object(free_hotspots, "store", self.db):
            batch = await free_hotspots.pct_batch("BK1", ["2026-09-10"], 2)
            kline = await free_hotspots.kline("BK1", 250)
        self.assertEqual(batch["2026-09-10"]["meta"]["return_basis"], "unadjusted_price_return")
        self.assertFalse(batch["2026-09-10"]["meta"]["strategy_backtest"])
        self.assertEqual(kline["y"][0][4], None)
        self.assertEqual(kline["y"][1][4], 10)
        self.assertEqual(kline["amount"], [1000, 1200])
        self.assertEqual(kline["turnover_ratio"], [2, 3])
        self.assertEqual(kline["amount_unit"], "CNY")


if __name__ == "__main__":
    unittest.main()


class FreshnessBoundaryTests(unittest.TestCase):
    def test_future_timestamp_after_close_is_not_accepted_as_settled(self):
        from datetime import timedelta
        now = datetime.now(free_hotspots.TZ).replace(hour=16, minute=0, second=0)
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        with patch.object(free_hotspots, 'datetime', FixedDateTime):
            result = free_hotspots._freshness((now + timedelta(hours=1)).isoformat(), day=now.date().isoformat(), realtime=True)
        self.assertEqual(result['status'], 'stale')


    def test_lunch_accepts_only_end_of_morning_quote(self):
        now = datetime.now(free_hotspots.TZ).replace(hour=12, minute=0, second=0)
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        with patch.object(free_hotspots, 'datetime', FixedDateTime):
            paused = free_hotspots._freshness(now.replace(hour=11, minute=30).isoformat(), day=now.date().isoformat(), realtime=True)
            stale = free_hotspots._freshness(now.replace(hour=11, minute=20).isoformat(), day=now.date().isoformat(), realtime=True)
        self.assertEqual(paused['status'], 'session_paused')
        self.assertEqual(stale['status'], 'stale')
