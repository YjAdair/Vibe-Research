"""社区功能测试：帖子/评论/点赞/关注/个人主页。"""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("ZZQUANT_COLLECTOR_MODE", "off")

from app.core.store import Store
from app.services import community, user_service


class CommunityTestCase(unittest.TestCase):
    _seq = 0

    @classmethod
    def _email(cls, name):
        cls._seq += 1
        return f"{name}{cls._seq}_{id(cls)}@test.com"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "db.sqlite"))
        self._orig_us = user_service.store
        self._orig_c = community.store
        user_service.store = self.store
        community.store = self.store
        user_service._ensure_tables()
        community._ensure_tables()
        # 两个用户
        self.alice = user_service.register(self._email("alice"), "password123", "Alice")["user_info"]
        self.bob = user_service.register(self._email("bob"), "password123", "Bob")["user_info"]

    def tearDown(self):
        user_service.store = self._orig_us
        community.store = self._orig_c
        self.tmp.cleanup()

    def test_post_lifecycle(self):
        created = community.create_post(self.alice["id"], "标题", "<p>内容<script>x</script></p>")
        pid = created["id"]
        detail = community.post_detail(pid, self.bob["id"])
        self.assertEqual(detail["title"], "标题")
        self.assertNotIn("script", detail["content"].lower())
        self.assertEqual(detail["author_nick"], "Alice")
        # 删除后不可见
        community.delete_post(self.alice["id"], pid)
        with self.assertRaises(ValueError):
            community.post_detail(pid)
        # 他人不能删
        created2 = community.create_post(self.alice["id"], "t2", "c2")
        with self.assertRaises(ValueError):
            community.delete_post(self.bob["id"], created2["id"])

    def test_post_pagination(self):
        for i in range(10):
            community.create_post(self.alice["id"], f"t{i}", "c")
        page1 = community.my_posts(self.alice["id"], 1, 8)
        self.assertEqual(page1["total"], 10)
        self.assertEqual(len(page1["items"]), 8)
        self.assertTrue(page1["has_more"])
        page2 = community.my_posts(self.alice["id"], 2, 8)
        self.assertEqual(len(page2["items"]), 2)
        self.assertFalse(page2["has_more"])

    def test_post_like_toggle(self):
        pid = community.create_post(self.alice["id"], "t", "c")["id"]
        r1 = community.toggle_post_like(self.bob["id"], pid)
        self.assertTrue(r1["liked"])
        self.assertEqual(r1["likes"], 1)
        r2 = community.toggle_post_like(self.bob["id"], pid)
        self.assertFalse(r2["liked"])
        self.assertEqual(r2["likes"], 0)
        # 重复点赞不会重复计数
        community.toggle_post_like(self.bob["id"], pid)
        community.toggle_post_like(self.bob["id"], pid)
        self.assertEqual(community.post_detail(pid)["likes"], 0)

    def test_comments_and_reactions(self):
        pid = community.create_post(self.alice["id"], "t", "c")["id"]
        cid = community.add_comment(self.bob["id"], pid, "好帖")["id"]
        comments = community.comment_list(pid, self.alice["id"])
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["content"], "好帖")
        self.assertEqual(comments[0]["author_nick"], "Bob")
        # Alice 赞
        r = community.toggle_comment_reaction(self.alice["id"], cid, "like")
        self.assertTrue(r["liked"])
        # Alice 改踩 → 赞取消、踩 +1
        r2 = community.toggle_comment_reaction(self.alice["id"], cid, "dislike")
        self.assertEqual(r2["likes"], 0)
        self.assertEqual(r2["dislikes"], 1)
        self.assertTrue(r2["disliked"])
        # 他人不能删评论
        with self.assertRaises(ValueError):
            community.delete_comment(self.alice["id"], cid)
        community.delete_comment(self.bob["id"], cid)
        self.assertEqual(community.comment_list(pid), [])

    def test_follow(self):
        # Bob 关注 Alice
        r = community.toggle_follow(self.bob["id"], self.alice["id"])
        self.assertTrue(r["followed"])
        self.assertEqual(r["followers"], 1)
        prof = community.profile_of(self.alice["id"], self.bob["id"])
        self.assertEqual(prof["followers"], 1)
        self.assertTrue(prof["followed_by_me"])
        # 列表
        followings = community.followings_of(self.bob["id"])
        self.assertEqual([u["user_nick"] for u in followings], ["Alice"])
        followers = community.followers_of(self.alice["id"])
        self.assertEqual([u["user_nick"] for u in followers], ["Bob"])
        # 不能关注自己
        with self.assertRaises(ValueError):
            community.toggle_follow(self.alice["id"], self.alice["id"])
        # 取消
        r2 = community.toggle_follow(self.bob["id"], self.alice["id"])
        self.assertFalse(r2["followed"])
        self.assertEqual(community.follower_count(self.alice["id"]), 0)

    def test_hot_posts(self):
        p1 = community.create_post(self.alice["id"], "t1", "c")["id"]
        p2 = community.create_post(self.bob["id"], "t2", "c")["id"]
        community.toggle_post_like(self.alice["id"], p2)
        hot = community.hot_posts(limit=10)
        self.assertEqual(hot[0]["id"], p2)
        self.assertTrue(all(p["status"] == "published" for p in hot))

    def test_empty_content_rejected(self):
        with self.assertRaises(ValueError):
            community.create_post(self.alice["id"], "", "   ")
        with self.assertRaises(ValueError):
            community.create_post(self.alice["id"], "", "<script>a</script>")


if __name__ == "__main__":
    unittest.main()
