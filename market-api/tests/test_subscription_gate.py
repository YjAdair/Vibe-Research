"""订阅墙鉴权测试：gated 接口 403/200 行为。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.core.store import Store
from app.services import user_service


class SubscriptionGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / "db.sqlite"))
        self._orig_store = user_service.store
        user_service.store = self.db
        user_service._ensure_tables()
        self._mode = settings.collector_mode
        settings.collector_mode = "off"
        from app.main import app
        self.client = TestClient(app)

    def tearDown(self):
        settings.collector_mode = self._mode
        user_service.store = self._orig_store
        self.tmp.cleanup()

    def _auth_headers(self, email="gate@test.com"):
        data = user_service.register(email, "password123")
        return {"Authorization": "Bearer " + data["token"]}

    def test_unauthenticated_gets_403_or_401_on_vip_kline(self):
        # 未登录：optional_user 返回 None -> 403
        r = self.client.get("/v3/api/sentiment/kline/day/1")
        self.assertIn(r.status_code, (401, 403))

    def test_free_model0_not_gated(self):
        r = self.client.get("/v3/api/sentiment/kline/day/0")
        self.assertNotEqual(r.status_code, 403)

    def test_gated_without_sub_then_with_sub(self):
        headers = self._auth_headers()
        # 未订阅 -> 403
        r = self.client.get("/v3/api/sentiment/kline/day/1", headers=headers)
        self.assertEqual(r.status_code, 403)
        # 开通订阅
        uid = user_service.login("gate@test.com", "password123")["user_info"]["id"]
        order = user_service.create_order(uid, "sentiment_vip", 1)
        user_service.pay_order(uid, order["order_id"])
        # 已订阅 -> 不再 403（可能 200 或数据 missing，但不是鉴权失败）
        r2 = self.client.get("/v3/api/sentiment/kline/day/1", headers=headers)
        self.assertNotEqual(r2.status_code, 403)

    def test_topic_kline_gate(self):
        headers = self._auth_headers("kline@test.com")
        r = self.client.get("/v3/topic/table/whatever/kline", headers=headers)
        self.assertEqual(r.status_code, 403)
        uid = user_service.login("kline@test.com", "password123")["user_info"]["id"]
        order = user_service.create_order(uid, "topic_kline", 1)
        user_service.pay_order(uid, order["order_id"])
        r2 = self.client.get("/v3/topic/table/whatever/kline", headers=headers)
        self.assertNotEqual(r2.status_code, 403)

    def test_pct_batch_trial_then_403_then_subscription(self):
        """pct/batch：未登录 401；登录后每日前 3 次免费；第 4 次 403；订阅后放行。"""
        url = "/v3/market/plates/17/801660/stocks/pct/batch?dates=2026-09-11&days=10"
        dummy = {"2026-09-11": {"intervals": [], "stocks": {}}}
        # 未登录 -> 401
        r = self.client.get(url)
        self.assertEqual(r.status_code, 401)
        headers = self._auth_headers("trial@test.com")
        from app.services import plate_flow
        with patch.object(plate_flow, "stocks_pct_batch", return_value=dummy):
            for i in range(3):
                r = self.client.get(url, headers=headers)
                self.assertEqual(r.status_code, 200, f"第{i+1}次免费试用应放行")
            # 第 4 次 -> 403
            r4 = self.client.get(url, headers=headers)
        self.assertEqual(r4.status_code, 403)
        # 订阅后不再受试用限制
        uid = user_service.login("trial@test.com", "password123")["user_info"]["id"]
        order = user_service.create_order(uid, "pct_interval_vip", 1)
        user_service.pay_order(uid, order["order_id"])
        with patch.object(plate_flow, "stocks_pct_batch", return_value=dummy):
            r5 = self.client.get(url, headers=headers)
        self.assertEqual(r5.status_code, 200)

    def test_pct_batch_trial_counter_isolated_per_user(self):
        """试用计数按用户隔离：A 用完 3 次不影响 B。"""
        url = "/v3/market/plates/17/801660/stocks/pct/batch?dates=2026-09-11&days=10"
        from app.services import plate_flow
        dummy = {"2026-09-11": {"intervals": [], "stocks": {}}}
        ha = self._auth_headers("triala@test.com")
        hb = self._auth_headers("trialb@test.com")
        with patch.object(plate_flow, "stocks_pct_batch", return_value=dummy):
            for _ in range(3):
                self.client.get(url, headers=ha)
            ra4 = self.client.get(url, headers=ha)
            rb1 = self.client.get(url, headers=hb)
        self.assertEqual(ra4.status_code, 403)
        self.assertEqual(rb1.status_code, 200)


if __name__ == "__main__":
    unittest.main()
