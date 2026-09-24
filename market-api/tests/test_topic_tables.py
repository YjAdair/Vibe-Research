import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.core.store import Store
from app.services import topic_tables


class TopicTableTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / "db.sqlite"))
        self.mode = settings.collector_mode
        settings.collector_mode = "external"
        self.db.daily_close_save("2026-09-10", [
            {"stock_code": "600869", "stock_name": "远东股份", "market_type": "沪", "concept": "光纤概念", "close": 24.1, "prev_close": 24.93},
            {"stock_code": "000001", "stock_name": "平安银行", "market_type": "深", "concept": "银行", "close": 11.2, "prev_close": 10.8},
        ], {"complete": True, "trade_date": "2026-09-10"})
        self.db.daily_close_save("2026-08-27", [
            {"stock_code": "600869", "stock_name": "远东股份", "market_type": "沪", "concept": "光纤概念", "close": 20.0, "prev_close": 19.5},
            {"stock_code": "000001", "stock_name": "平安银行", "market_type": "深", "concept": "银行", "close": 10.0, "prev_close": 9.8},
        ], {"complete": True, "trade_date": "2026-08-27"})

    def tearDown(self):
        settings.collector_mode = self.mode
        from app.main import app
        app.dependency_overrides.clear()
        self.tmp.cleanup()

    async def test_create_list_follow_and_quote_from_published_close(self):
        with patch.object(topic_tables, "store", self.db):
            created = topic_tables.create_table({
                "name": "光纤主线",
                "content": "CPO + 光纤",
                "rows": [{"一级大类": "科技", "二级小类": "光模块", "个股": "远东股份", "相关性": "龙头"}],
            })
            self.assertEqual(created["name"], "光纤主线")
            self.assertEqual(created["rows"][0]["股票代码"], "600869")
            self.assertIsNotNone(created["rows"][0]["涨跌幅"])
            listed = topic_tables.list_tables()
            self.assertEqual(listed["total"], 1)
            self.assertEqual(listed["items"][0]["id"], created["unique_key"])
            followed = topic_tables.toggle_follow(created["unique_key"], True, user_id="u1")
            self.assertEqual(followed["status"], "ok")
            mine = topic_tables.follows(user_id="u1")
            self.assertEqual(mine["total"], 1)
            quotes = topic_tables.stock_returns(["远东股份", "没有这只"])
            self.assertEqual(quotes["data"]["远东股份"]["symbol_code"], "600869")
            self.assertNotIn("没有这只", quotes["data"])

    async def test_api_user_table_does_not_use_market_rank_shape(self):
        from app.main import app
        import httpx
        from app.core import deps
        app.dependency_overrides[deps.current_user] = lambda: {"id": "u1"}
        app.dependency_overrides[deps.optional_user] = lambda: {"id": "u1"}
        with patch("app.core.store.store", self.db), patch.object(topic_tables, "store", self.db):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                created = (await client.post("/v3/topic/table/create", json={
                    "name": "光纤主线",
                    "content": "测试",
                    "rows": [{"一级大类": "科技", "个股": "远东股份"}],
                })).json()
                key = created["data"]["unique_key"]
                listed = (await client.get("/v3/topic/tables")).json()
                detail = (await client.get(f"/v3/topic/table/{key}")).json()
                followed = (await client.post(f"/v3/topic/follows/{key}")).json()
                mine = (await client.get("/v3/topic/follows")).json()
                quotes = (await client.post("/v3/topic/table/stock-returns", json={"stocks": ["远东股份"]})).json()
        self.assertEqual(created["code"], 20000)
        self.assertEqual(listed["data"]["total"], 1)
        self.assertEqual(listed["data"]["items"][0]["name"], "光纤主线")
        self.assertEqual(detail["data"]["rows"][0]["个股"], "远东股份")
        self.assertEqual(followed["data"]["is_followed"], True)
        self.assertEqual(mine["data"]["total"], 1)
        self.assertEqual(quotes["data"]["data"]["远东股份"]["symbol_code"], "600869")

