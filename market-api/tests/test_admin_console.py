import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.store import Store
from app.services import user_service


class AdminConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'db.sqlite'))
        self._orig_store = user_service.store
        user_service.store = self.db
        user_service._ensure_tables()

    def tearDown(self):
        user_service.store = self._orig_store
        self.tmp.cleanup()

    def _register(self, email):
        with patch.object(user_service, 'store', self.db):
            return user_service.register(email, 'password123')

    def test_grant_revoke_and_guard(self):
        acc = self._register('admin1@test.com')
        uid = acc['user_info']['id']
        self.assertFalse(user_service.has_subscription(uid, 'sentiment_vip'))
        granted = user_service.admin_grant_subscription(uid, 'sentiment_vip', 1)
        self.assertTrue(granted['expires_at'] > '2026')
        self.assertTrue(user_service.has_subscription(uid, 'sentiment_vip'))
        user_service.admin_revoke_subscription(uid, 'sentiment_vip')
        self.assertFalse(user_service.has_subscription(uid, 'sentiment_vip'))

    def test_grant_validates_module_and_user(self):
        acc = self._register('admin2@test.com')
        uid = acc['user_info']['id']
        with self.assertRaises(ValueError):
            user_service.admin_grant_subscription(uid, 'no_such_module', 1)
        with self.assertRaises(ValueError):
            user_service.admin_grant_subscription('ghost', 'sentiment_vip', 1)

    def test_role_change_and_orders(self):
        acc = self._register('admin3@test.com')
        uid = acc['user_info']['id']
        self.assertEqual(user_service.admin_set_role(uid, 'admin')['role'], 'admin')
        with self.assertRaises(ValueError):
            user_service.admin_set_role(uid, 'superadmin')
        user_service.create_order(uid, 'ai_premarket', 1)
        orders = user_service.admin_list_orders()
        self.assertEqual(orders['total'], 1)
        self.assertEqual(orders['items'][0]['module_code'], 'ai_premarket')
        self.assertEqual(orders['items'][0]['email'], 'admin3@test.com')
        users = user_service.admin_list_users()
        self.assertEqual(users['total'], 1)
        self.assertIn('subscriptions', users['items'][0])

    def test_guard_rejects_non_admin(self):
        acc = self._register('normal@test.com')
        uid = acc['user_info']['id']
        tok = acc['token']
        with patch.object(user_service, 'store', self.db):
            from app.main import app
            client = TestClient(app)
            r = client.get('/v3/admin/users', headers={'Authorization': 'Bearer ' + tok})
            self.assertEqual(r.status_code, 403)
            user_service.admin_set_role(uid, 'admin')
            r2 = client.get('/v3/admin/users', headers={'Authorization': 'Bearer ' + tok})
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(r2.json()['code'], 20000)
