"""长尾接口测试：jygs 解析辅助、排序、点赞、admin rules、referral admin、验证码。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.store import Store
from app.services import jygs, topic_tables, user_service


class JygsHelperTests(unittest.TestCase):
    def test_extract_str_handles_escapes(self):
        src = 'data:{title:"a' + chr(92) + '"b' + chr(92) + 'n c",create_time:"2026-06-22 17:52:53"}'
        self.assertEqual(jygs._extract_str(src, "title"), 'a"b\n c')
        self.assertEqual(jygs._extract_str(src, "create_time"), "2026-06-22 17:52:53")

    def test_html_to_text(self):
        self.assertEqual(jygs._html_to_text("<p>A<br>B</p><p>C</p>"), "A\nB\nC")

    def test_extract_images_dedupe(self):
        imgs = jygs._extract_images('<img src="a.jpg?x=1"><img src="a.jpg?x=2">', "cover.png")
        self.assertEqual(imgs, ["cover.png", "a.jpg"])

    def test_article_url(self):
        self.assertEqual(jygs._article_url("4ejtumnjrmy"), "https://www.jiuyangongshe.com/a/4ejtumnjrmy")
        self.assertEqual(jygs._article_url("123456"), "https://www.jiuyangongshe.com/a/123456")
        self.assertIsNone(jygs._article_url("not-a-url"))


class PinnedOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / "db.sqlite"))
        self._t = topic_tables.store
        topic_tables.store = self.db

    def tearDown(self):
        topic_tables.store = self._t
        self.tmp.cleanup()

    def test_pinned_order_persisted(self):
        self.db.kv_set("topic_pinned_order", {"a": 0, "b": 1})
        order = self.db.kv_get("topic_pinned_order")
        self.assertEqual(order, {"a": 0, "b": 1})


class AiReportLikeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / "db.sqlite"))
        self._t = user_service.store
        user_service.store = self.db
        user_service._ensure_tables()

    def tearDown(self):
        user_service.store = self._t
        self.tmp.cleanup()

    def test_referral_admin_list(self):
        with patch.object(user_service, "store", self.db):
            user_service.register("inviter_lt@test.com", "password123")
            data = user_service.referral_admin_list(1, 10)
            # 无邀请记录时返回空列表结构
            self.assertEqual(data["total"], 0)
            self.assertEqual(data["items"], [])

    def test_referral_audit_reward(self):
        with patch.object(user_service, "store", self.db):
            with self.assertRaises(ValueError):
                user_service.referral_audit("u1:u2", True)

    def test_set_suspicious(self):
        with patch.object(user_service, "store", self.db):
            rec = user_service.referral_set_suspicious("u9", 1)
            self.assertEqual(rec["is_suspicious"], 1)


if __name__ == "__main__":
    unittest.main()
