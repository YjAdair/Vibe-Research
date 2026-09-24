"""第 40 轮补齐缺口端点：API 令牌、积分调整（真实 sqlite 临时库）。"""
import tempfile
import unittest
from pathlib import Path

from app.core.store import Store
from app.services import user_service


class GapEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / "db.sqlite"))
        self._orig = user_service.store
        user_service.store = self.db
        user_service._ensure_tables()
        with self.db._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO users (id, email, password_hash, points) VALUES ('u1', 'u1@t.co', 'x', 100)")

    def tearDown(self):
        user_service.store = self._orig
        self.tmp.cleanup()

    async def test_token_lazy_and_stable(self):
        first = user_service.api_token_get('u1')
        second = user_service.api_token_get('u1')
        self.assertTrue(first['token'].startswith('zzq_'))
        self.assertEqual(first['token'], second['token'])

    async def test_token_refresh_invalidates_old(self):
        t1 = user_service.api_token_get('u1')
        t2 = user_service.api_token_refresh('u1')
        t3 = user_service.api_token_get('u1')
        self.assertNotEqual(t1['token'], t2['token'])
        self.assertEqual(t2['token'], t3['token'])

    async def test_points_add_and_deduct(self):
        out = user_service.points_adjust('u1', 50, 'add')
        self.assertEqual(out['points'], 150)
        out2 = user_service.points_adjust('u1', 30, 'deduct')
        self.assertEqual(out2['points'], 120)
        self.assertEqual(out2['delta'], -30)

    async def test_points_invalid_action_rejected(self):
        with self.assertRaises(ValueError):
            user_service.points_adjust('u1', 50, 'steal')

    async def test_points_zero_amount_rejected(self):
        with self.assertRaises(ValueError):
            user_service.points_adjust('u1', 0, 'add')

    async def test_points_insufficient_rejected(self):
        with self.assertRaises(ValueError):
            user_service.points_adjust('u1', 500, 'deduct')

    async def test_points_unknown_user_rejected(self):
        with self.assertRaises(ValueError):
            user_service.points_adjust('ghost', 50, 'add')


if __name__ == '__main__':
    unittest.main()
