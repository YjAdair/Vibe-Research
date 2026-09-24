import tempfile
import unittest
from pathlib import Path

from app.core.store import Store
from app.services import user_service


class PointsRedemptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / "db.sqlite"))
        self._orig_store = user_service.store
        user_service.store = self.db
        user_service._ensure_tables()

    def tearDown(self):
        user_service.store = self._orig_store
        self.tmp.cleanup()

    def _user(self, email, points=0):
        uid = user_service.register(email, "password123")["user_info"]["id"]
        if points:
            with self.db._conn() as conn:
                conn.execute("UPDATE users SET points = ? WHERE id = ?", (points, uid))
        return uid

    def _points(self, uid):
        with self.db._conn() as conn:
            return conn.execute("SELECT points FROM users WHERE id = ?", (uid,)).fetchone()["points"]

    def test_balance_below_cap_uses_full_balance(self):
        # 299 yuan order, 5000 points -> can only cover 50 yuan.
        uid = self._user("p1@test.com", points=5000)
        o = user_service.create_order(uid, "sentiment_vip", 1, use_points=True)
        self.assertEqual(o["amount"], 249.0)
        o_row = user_service.list_orders(uid)[0]
        self.assertEqual(o_row["points_used"], 5000)

    def test_balance_above_cap_caps_at_half(self):
        # 299 yuan order, 50000 points -> cap is 149.5 yuan = 14950 points.
        uid = self._user("p2@test.com", points=50000)
        o = user_service.create_order(uid, "sentiment_vip", 1, use_points=True)
        self.assertEqual(o["amount"], 149.5)
        self.assertEqual(user_service.list_orders(uid)[0]["points_used"], 14950)

    def test_no_deduction_before_payment(self):
        uid = self._user("p3@test.com", points=5000)
        user_service.create_order(uid, "sentiment_vip", 1, use_points=True)
        self.assertEqual(self._points(uid), 5000)

    def test_deduct_on_pay_and_zero_points(self):
        uid = self._user("p4@test.com", points=5000)
        o = user_service.create_order(uid, "sentiment_vip", 1, use_points=True)
        user_service.pay_order(uid, o["order_id"])
        self.assertEqual(self._points(uid), 0)

    def test_without_use_points_no_deduction(self):
        uid = self._user("p5@test.com", points=5000)
        o = user_service.create_order(uid, "sentiment_vip", 1)
        self.assertEqual(o["amount"], 299.0)
        self.assertEqual(user_service.list_orders(uid)[0]["points_used"], 0)
        user_service.pay_order(uid, o["order_id"])
        self.assertEqual(self._points(uid), 5000)

    def test_idempotent_pay_no_double_deduction(self):
        uid = self._user("p6@test.com", points=5000)
        o = user_service.create_order(uid, "sentiment_vip", 1, use_points=True)
        user_service.pay_order(uid, o["order_id"])
        user_service.pay_order(uid, o["order_id"])  # already paid path
        self.assertEqual(self._points(uid), 0)


if __name__ == "__main__":
    unittest.main()
