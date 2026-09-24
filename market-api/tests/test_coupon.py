import tempfile
import unittest
from pathlib import Path

from app.core.store import Store
from app.services import user_service


class CouponTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'db.sqlite'))
        self._orig_store = user_service.store
        user_service.store = self.db
        user_service._ensure_tables()

    def tearDown(self):
        user_service.store = self._orig_store
        self.tmp.cleanup()

    def _user(self, email):
        return user_service.register(email, 'password123')['user_info']['id']

    def test_coupon_discount_and_single_redemption(self):
        uid = self._user('c1@test.com')
        cp = user_service.admin_create_coupon(80, 2, 30)  # 20% off
        # validate ok
        v = user_service.validate_coupon(uid, cp['code'])
        self.assertTrue(v['valid'])
        # order with coupon: 299 -> 239.2
        o = user_service.create_order(uid, 'sentiment_vip', 1, cp['code'])
        self.assertEqual(o['amount'], 239.2)
        user_service.pay_order(uid, o['order_id'])
        # used once, cannot validate again for same user
        v2 = user_service.validate_coupon(uid, cp['code'])
        self.assertFalse(v2['valid'])

    def test_coupon_shared_between_users_until_max_uses(self):
        u1 = self._user('c2@test.com')
        u2 = self._user('c3@test.com')
        cp = user_service.admin_create_coupon(50, 1, 30)
        o1 = user_service.create_order(u1, 'ai_premarket', 1, cp['code'])
        self.assertEqual(o1['amount'], 9.5)
        user_service.pay_order(u1, o1['order_id'])
        # second user: max_uses exhausted
        v = user_service.validate_coupon(u2, cp['code'])
        self.assertFalse(v['valid'])

    def test_expired_and_unknown_coupon(self):
        uid = self._user('c4@test.com')
        self.assertFalse(user_service.validate_coupon(uid, 'NOSUCH')['valid'])
        cp = user_service.admin_create_coupon(100, 1, days_valid=0)
        # days_valid clamps to >= 1; force expire via direct update
        with self.db._conn() as conn:
            conn.execute("UPDATE coupons SET expires_at = '2000-01-01T00:00:00' WHERE code = ?", (cp['code'],))
        v = user_service.validate_coupon(uid, cp['code'])
        self.assertFalse(v['valid'])

    def test_duplicate_code_rejected(self):
        cp = user_service.admin_create_coupon(90, 1, 30, code='FIXED01')
        self.assertEqual(cp['code'], 'FIXED01')
        with self.assertRaises(ValueError):
            user_service.admin_create_coupon(90, 1, 30, code='FIXED01')
