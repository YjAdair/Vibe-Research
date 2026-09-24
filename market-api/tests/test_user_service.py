import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app.core import auth
from app.core.store import Store
from app.services import user_service


class AuthUtilTests(unittest.TestCase):
    def test_password_hash_roundtrip(self):
        h = auth.hash_password("secret123!")
        self.assertTrue(h.startswith("pbkdf2$"))
        self.assertTrue(auth.verify_password("secret123!", h))
        self.assertFalse(auth.verify_password("wrong", h))

    def test_jwt_roundtrip(self):
        tok = auth.issue_token("user1", "access")
        payload = auth.verify_token(tok, "access")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["sub"], "user1")
        self.assertIsNone(auth.verify_token(tok, "refresh"))
        self.assertIsNone(auth.verify_token("bad.token.sig", "access"))
        expired = auth.issue_token("user1", "access", ttl=-10)
        self.assertIsNone(auth.verify_token(expired, "access"))


class UserServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / "db.sqlite"))
        self._orig_store = user_service.store
        user_service.store = self.db
        user_service._ensure_tables()

    def tearDown(self):
        user_service.store = self._orig_store
        self.tmp.cleanup()

    def test_register_login_flow(self):
        data = user_service.register("a@test.com", "password123", "阿甲")
        self.assertEqual(data["user_info"]["email"], "a@test.com")
        self.assertTrue(data["token"] and data["refresh_token"])
        with self.assertRaises(ValueError) as ctx:
            user_service.register("a@test.com", "password123")
        self.assertIn("已注册", str(ctx.exception))
        with self.assertRaises(ValueError):
            user_service.register("b@test.com", "short")
        data2 = user_service.login("a@test.com", "password123")
        self.assertEqual(data2["user_info"]["id"], data["user_info"]["id"])
        with self.assertRaises(ValueError):
            user_service.login("a@test.com", "wrongpass11")

    def test_order_subscription_flow(self):
        data = user_service.register("buyer@test.com", "password123")
        uid = data["user_info"]["id"]
        self.assertEqual(user_service.list_subscriptions(uid), [])
        self.assertFalse(user_service.has_subscription(uid, "sentiment_vip"))
        order = user_service.create_order(uid, "sentiment_vip", 2)
        self.assertEqual(order["amount"], 598.0)
        self.assertEqual(order["status"], "pending")
        with self.assertRaises(ValueError):
            user_service.create_order(uid, "nonexistent")
        paid = user_service.pay_order(uid, order["order_id"])
        self.assertEqual(paid["status"], "paid")
        self.assertTrue(user_service.has_subscription(uid, "sentiment_vip"))
        subs = user_service.list_subscriptions(uid)
        self.assertTrue(any(s["module_code"] == "sentiment_vip" and s["active"] for s in subs))
        paid2 = user_service.pay_order(uid, order["order_id"])
        self.assertTrue(paid2.get("already"))
        orders = user_service.list_orders(uid)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["status"], "paid")
        order2 = user_service.create_order(uid, "sentiment_vip", 1)
        user_service.pay_order(uid, order2["order_id"])
        subs2 = user_service.list_subscriptions(uid)
        vip = next(s for s in subs2 if s["module_code"] == "sentiment_vip")
        exp = datetime.fromisoformat(vip["expires_at"])
        self.assertGreater(exp, datetime.now() + timedelta(days=60))



class TestUpdateEmail(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / "db.sqlite"))
        self._orig_store = user_service.store
        user_service.store = self.db
        user_service._ensure_tables()

    def tearDown(self):
        user_service.store = self._orig_store
        self.tmp.cleanup()

    def test_update_email_roundtrip(self):
        data = user_service.register("bindtest@x.com", "password12345", "换绑测试")
        uid = data["user_info"]["id"]
        # 换绑
        view = user_service.update_email(uid, "bindnew@x.com")
        self.assertEqual(view["email"], "bindnew@x.com")
        # 旧邮箱不能再登录，新邮箱可以
        login1 = user_service.login("bindnew@x.com", "password12345")
        self.assertIn("token", login1)
        with self.assertRaises(ValueError):
            user_service.login("bindtest@x.com", "password12345")

    def test_email_conflict(self):
        user_service.register("bindconflict@x.com", "password12345", "冲突A")
        data = user_service.register("bindconflictb@x.com", "password12345", "冲突B")
        uid = data["user_info"]["id"]
        with self.assertRaises(ValueError):
            user_service.update_email(uid, "bindconflict@x.com")

if __name__ == "__main__":
    unittest.main()

