import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.core import deps
from app.core.store import Store
from app.main import app
from app.services import ml_r1, ml_r1_service, topic, topic_follow, topic_tables


class HotspotTopicSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / "db.sqlite"))
        self.overrides = {}

    def tearDown(self):
        app.dependency_overrides.clear()
        self.tmp.cleanup()

    async def test_follow_and_unread_are_isolated_and_anonymous_is_empty(self):
        with patch.object(topic_tables, "store", self.db), patch.object(topic_follow, "store", self.db):
            item = topic_tables.create_table({"name": "隔离题材", "rows": [{"个股": "甲"}]}, user_id="u1")
            key = item["unique_key"]
            self.db.topic_snapshot_save("2026-09-09", [{"unique_key": key, "name": "隔离题材", "today_pct": 1.0, "limit_up_count": 1, "up_ratio": 50}])
            self.db.topic_snapshot_save("2026-09-10", [{"unique_key": key, "name": "隔离题材", "today_pct": 3.0, "limit_up_count": 2, "up_ratio": 70}])
            with self.db._conn() as conn:
                conn.execute("UPDATE topic_snapshots SET payload=json_set(payload, '$.formula_version', ?, '$.input_snapshot_id', 'test')", (ml_r1.VERSION,))
            app.dependency_overrides[deps.current_user] = lambda: {"id": "u1"}
            app.dependency_overrides[deps.optional_user] = lambda: {"id": "u1"}
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                self.assertEqual((await client.post(f"/v3/topic/follows/{key}")).status_code, 200)
                u1 = (await client.get("/v3/topic/follows/status", params={"topic_ids": key})).json()
                unread_u1 = (await client.get("/v3/topic/follows")).json()
                app.dependency_overrides[deps.current_user] = lambda: {"id": "u2"}
                app.dependency_overrides[deps.optional_user] = lambda: {"id": "u2"}
                u2 = (await client.get("/v3/topic/follows/status", params={"topic_ids": key})).json()
                unread_u2 = (await client.get("/v3/topic/follows")).json()
                app.dependency_overrides.pop(deps.current_user)
                app.dependency_overrides.pop(deps.optional_user)
                anonymous = (await client.get("/v3/topic/follows")).json()
                denied = await client.post(f"/v3/topic/follows/{key}")
        self.assertEqual(u1["data"]["followed_topic_ids"], [key])
        self.assertEqual(u2["data"]["followed_topic_ids"], [])
        self.assertEqual(unread_u1["data"]["unread_count"], 1)
        self.assertEqual(unread_u2["data"]["unread_count"], 0)
        self.assertEqual(anonymous["data"]["items"], [])
        self.assertEqual(denied.status_code, 401)

    async def test_custom_table_does_not_replay_current_members_in_historical_popularity(self):
        with patch.object(topic_tables, "store", self.db), patch.object(topic, "store", self.db):
            item = topic_tables.create_table({"name": "当前成员", "rows": [{"个股": "甲"}]}, user_id="u1")
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                body = (await client.get(f"/v3/topic/table/{item['unique_key']}/stocks/popular", params={"date1": "2026-09-01"})).json()
        self.assertEqual(body["data"]["status"], "missing_historical_members")
        self.assertEqual(body["data"]["items"], [])

    async def test_rank_compounds_published_days_and_keeps_missing_values(self):
        self.db.topic_snapshot_save("2026-09-08", [
            {"unique_key": "BK1", "name": "A", "score": 10, "today_pct": 5},
            {"unique_key": "BK2", "name": "缺值", "score": 40, "today_pct": 1},
        ])
        self.db.topic_snapshot_save("2026-09-09", [
            {"unique_key": "BK1", "name": "A", "score": 20, "today_pct": -10},
            {"unique_key": "BK2", "name": "缺值", "score": 40, "today_pct": 1},
        ])
        self.db.topic_snapshot_save("2026-09-10", [
            {"unique_key": "BK1", "name": "A", "score": 30, "today_pct": 10},
            {"unique_key": "BK2", "name": "缺值", "score": 40, "today_pct": None},
            {"unique_key": "BK3", "name": "未来成员", "score": 99, "today_pct": 99},
        ])
        self.db.kv_set("collector_calendar_v1", {"complete": True, "days": ["2026-09-08", "2026-09-09", "2026-09-10"]})
        with self.db._conn() as conn:
            conn.execute("UPDATE topic_snapshots SET payload=json_set(payload, '$.formula_version', ?, '$.input_snapshot_id', 'test')", (ml_r1.VERSION,))
        with patch.object(topic_tables, "store", self.db), patch.object(ml_r1_service, "store", self.db):
            result = topic_tables.rank_tables("2026-09-10", n_days=3, limit=10, sort_by="rate")
            missing = topic_tables.rank_tables("2026-09-10", n_days=5)
        self.assertAlmostEqual(result["items"][0]["sum_rate"], 3.95, places=2)
        self.assertEqual(result["items"][1]["status"], "missing")
        self.assertIsNone(result["items"][1]["sum_rate"])
        self.assertEqual(next(row for row in result["items"] if row["topic_unique_key"] == "BK3")["status"], "missing")
        self.assertEqual(missing["status"], "missing")
        self.assertEqual(missing["items"], [])

    async def test_kline_is_daily_nav_only_when_ml_r1_returns_are_continuous(self):
        days = ["2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14"]
        self.db.kv_set("collector_calendar_v1", {"complete": True, "verified_at": "2026-09-14T21:00:00+08:00", "days": days})
        for day, rate in zip(days, [0.01, -0.01, 0.02, 0.0, 0.03]):
            self.db.ml_r1_snapshot_save("eastmoney_native_v1", day, {
                "date": day, "source": "local_independent", "source_evidence": "test",
                "formula_version": ml_r1.VERSION, "status": "final", "rows": [{
                    "plate_code": "BK1", "plate_name": "A", "return": rate,
                    "member_returns": {"000001": {"return": rate, "stock_name": "甲"}},
                    "membership_as_of": day + "T09:00:00+08:00",
                    "metric_status": {"return": "final"},
                }],
            })
        self.db.topic_snapshot_save("2026-09-14", [{"unique_key": "BK1", "name": "A"}])
        app.dependency_overrides[deps.optional_user] = lambda: {"id": "u1"}
        with patch.object(topic_tables, "store", self.db), patch.object(ml_r1_service, "store", self.db), \
                patch.object(topic, "store", self.db), patch("app.services.user_service.has_subscription", return_value=True):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                body = (await client.get("/v3/topic/table/BK1/kline", params={"date1": "2026-09-14", "n": 5})).json()
        data = body["data"]
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["series_kind"], "daily_nav")
        self.assertTrue(all(not isinstance(value, list) for value in data["y"]))
        self.assertNotIn("ohlc", data)
        self.assertNotIn("OHLC", data)


if __name__ == "__main__":
    unittest.main()
