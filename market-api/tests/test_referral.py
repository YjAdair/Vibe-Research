import tempfile
import unittest
from pathlib import Path

from app.core.store import Store
from app.services import user_service


class ReferralTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'db.sqlite'))
        self._orig_store = user_service.store
        user_service.store = self.db
        user_service._ensure_tables()

    def tearDown(self):
        user_service.store = self._orig_store
        self.tmp.cleanup()

    def test_invite_bind_and_first_paid_order_reward(self):
        inviter = user_service.register('inviter@test.com', 'password123')
        with self.db._conn() as conn:
            code = conn.execute(
                "SELECT invite_code FROM users WHERE email='inviter@test.com'"
            ).fetchone()['invite_code']
        invitee = user_service.register('invitee@test.com', 'password123', invite_code=code)
        iid = invitee['user_info']['id']
        # bind happened
        with self.db._conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM referrals WHERE invitee_id=?", (iid,)).fetchone()[0]
        self.assertEqual(n, 1)
        # first paid order -> inviter +20 points, exactly once
        oid = user_service.create_order(iid, 'ai_premarket', 1)['order_id']
        user_service.pay_order(iid, oid)
        stats = user_service.referral_stats(inviter['user_info']['id'])
        self.assertEqual(stats['count'], 1)
        self.assertEqual(stats['reward_points'], 20)
        # second order: no double reward
        oid2 = user_service.create_order(iid, 'ai_premarket', 1)['order_id']
        user_service.pay_order(iid, oid2)
        stats2 = user_service.referral_stats(inviter['user_info']['id'])
        self.assertEqual(stats2['reward_points'], 20)

    def test_bad_invite_code_ignored(self):
        acc = user_service.register('plain@test.com', 'password123', invite_code='nosuchcode')
        with self.db._conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
            iu = conn.execute("SELECT invite_code FROM users WHERE id=?", (acc['user_info']['id'],)).fetchone()['invite_code']
        self.assertTrue(iu)  # every user still gets own invite code
        self.assertEqual(n, 0)
        stats = user_service.referral_stats(acc['user_info']['id'])
        self.assertEqual((stats['count'], stats['reward_points']), (0, 0))
