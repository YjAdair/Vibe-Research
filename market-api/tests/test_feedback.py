"""意见反馈与荣誉殿堂测试。"""
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import feedback  # noqa: E402


USER = {"id": "u1", "user_nick": "张三", "role": "user"}
ADMIN = {"id": "u9", "user_nick": "管理员", "role": "admin"}
USER2 = {"id": "u2", "user_nick": "李四", "role": "user"}


class TestFeedback(unittest.TestCase):
    def test_submit_and_list(self):
        feedback.submit(USER, "界面很好用")
        feedback.submit(USER2, "建议增加龙虎榜筛选")
        rows = feedback.list_feedbacks(1, 10)
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(rows[0]["username"], "李四")

    def test_empty_content_rejected(self):
        with self.assertRaises(ValueError):
            feedback.submit(USER, "   ")

    def test_delete_own_only(self):
        r = feedback.submit(USER, "待删除")
        with self.assertRaises(ValueError):
            feedback.delete(USER2, r["id"])
        feedback.delete(USER, r["id"])
        rows = [x for x in feedback.list_feedbacks(1, 100) if x["id"] == r["id"]]
        self.assertEqual(len(rows), 0)

    def test_toggle_like(self):
        r = feedback.submit(USER, "点赞测试")
        res = feedback.toggle_like(USER2, r["id"])
        self.assertEqual(res["likes"], 1)
        self.assertTrue(res["liked"])
        res = feedback.toggle_like(USER2, r["id"])
        self.assertEqual(res["likes"], 0)
        self.assertFalse(res["liked"])

    def test_comment_and_like(self):
        r = feedback.submit(USER, "评论测试")
        c = feedback.add_comment(USER2, r["id"], "同意")
        rows = feedback.list_feedbacks(1, 10, viewer_id="u2")
        hit = [x for x in rows if x["id"] == r["id"]][0]
        self.assertEqual(len(hit["comments"]), 1)
        self.assertEqual(hit["comments"][0]["content"], "同意")
        res = feedback.toggle_comment_like(USER, c["id"])
        self.assertEqual(res["likes"], 1)


class TestHallOfFame(unittest.TestCase):
    def test_admin_upload_and_list(self):
        r = feedback.hall_upload(ADMIN, "/static/hall/demo1.png", "2026-09 捐赠截图")
        self.assertGreater(r["id"], 0)
        data = feedback.hall_list(1, 24)
        self.assertGreaterEqual(data["total"], 1)
        self.assertTrue(any(x["image_path"] == "/static/hall/demo1.png" for x in data["list"]))

    def test_non_admin_rejected(self):
        with self.assertRaises(PermissionError):
            feedback.hall_upload(USER, "/static/hall/x.png")

    def test_admin_delete(self):
        r = feedback.hall_upload(ADMIN, "/static/hall/tmp.png")
        feedback.hall_delete(ADMIN, r["id"])
        data = feedback.hall_list(1, 100)
        self.assertFalse(any(x["id"] == r["id"] for x in data["list"]))



class TestHallUploadCreatedTime(unittest.TestCase):
    def test_created_time_preserved(self):
        r = feedback.hall_upload(ADMIN, "/static/hall/t1.png", "带日期", "2026-09-01 12:00:00")
        data = feedback.hall_list(1, 100)
        hit = [x for x in data["list"] if x["id"] == r["id"]]
        self.assertTrue(hit)
        self.assertEqual(hit[0]["created_time"], "2026-09-01 12:00:00")
        feedback.hall_delete(ADMIN, r["id"])

if __name__ == "__main__":
    unittest.main()
