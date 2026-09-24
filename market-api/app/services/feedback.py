"""意见反馈：轻量留言板（内容 + 点赞 + 评论），协议对齐原站 /feedback/*。"""
from __future__ import annotations

import threading
from datetime import datetime

from app.core.store import store

_lock = threading.Lock()
_tables_ready = False


def _ensure_tables() -> None:
    global _tables_ready
    if _tables_ready:
        return
    with _lock:
        if _tables_ready:
            return
        with store._conn() as conn:  # noqa: SLF001
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS feedbacks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    username TEXT DEFAULT '',
                    content TEXT NOT NULL,
                    images TEXT DEFAULT '[]',
                    likes INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'published',
                    created_time TEXT,
                    updated_time TEXT
                );
                CREATE TABLE IF NOT EXISTS feedback_likes (
                    user_id TEXT NOT NULL,
                    feedback_id INTEGER NOT NULL,
                    PRIMARY KEY (user_id, feedback_id)
                );
                CREATE TABLE IF NOT EXISTS feedback_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feedback_id INTEGER NOT NULL,
                    user_id TEXT NOT NULL,
                    username TEXT DEFAULT '',
                    content TEXT NOT NULL,
                    likes INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'published',
                    created_time TEXT
                );
                CREATE TABLE IF NOT EXISTS feedback_comment_likes (
                    user_id TEXT NOT NULL,
                    comment_id INTEGER NOT NULL,
                    PRIMARY KEY (user_id, comment_id)
                );
                CREATE INDEX IF NOT EXISTS idx_feedbacks_created ON feedbacks(status, created_time DESC);
                CREATE INDEX IF NOT EXISTS idx_fb_comments ON feedback_comments(feedback_id, status, created_time ASC);
                CREATE TABLE IF NOT EXISTS hall_of_fame_images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_path TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    sort_order INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'published',
                    created_time TEXT
                );
                """
            )
        _tables_ready = True


_ensure_tables()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _user_label(user: dict | None) -> tuple[str, str]:
    if not user:
        return ("", "")
    return (str(user["id"]), str(user.get("user_nick") or user.get("email") or "用户"))


def submit(user: dict | None, content: str, images: list[str] | None = None) -> dict:
    _ensure_tables()
    content = (content or "").strip()
    if not content:
        raise ValueError("content required")
    if len(content) > 2000:
        raise ValueError("content too long")
    uid, uname = _user_label(user)
    now = _now()
    import json
    with store._conn() as conn:  # noqa: SLF001
        cur = conn.execute(
            "INSERT INTO feedbacks(user_id, username, content, images, created_time, updated_time) VALUES(?,?,?,?,?,?)",
            (uid, uname, content, json.dumps(images or [], ensure_ascii=False), now, now),
        )
        return {"id": int(cur.lastrowid)}


def list_feedbacks(page: int = 1, limit: int = 10, viewer_id: str = "") -> list[dict]:
    _ensure_tables()
    import json
    offset = max(page - 1, 0) * limit
    with store._conn() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT * FROM feedbacks WHERE status='published' ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        ph = ",".join("?" * len(ids))
        comments = conn.execute(
            f"SELECT * FROM feedback_comments WHERE feedback_id IN ({ph}) AND status='published' ORDER BY id ASC",
            ids,
        ).fetchall()
        liked = set()
        if viewer_id:
            for r in conn.execute(
                f"SELECT feedback_id FROM feedback_likes WHERE user_id=? AND feedback_id IN ({ph})",
                [viewer_id, *ids],
            ).fetchall():
                liked.add(r["feedback_id"])
        cliked = set()
        if viewer_id:
            cids = [c["id"] for c in comments]
            if cids:
                cph = ",".join("?" * len(cids))
                for r in conn.execute(
                    f"SELECT comment_id FROM feedback_comment_likes WHERE user_id=? AND comment_id IN ({cph})",
                    [viewer_id, *cids],
                ).fetchall():
                    cliked.add(r["comment_id"])
    by_fb: dict[int, list[dict]] = {}
    for c in comments:
        item = dict(c)
        item["liked_by_me"] = c["id"] in cliked
        by_fb.setdefault(c["feedback_id"], []).append(item)
    out = []
    for r in rows:
        item = dict(r)
        try:
            item["images"] = json.loads(item.get("images") or "[]")
        except Exception:
            item["images"] = []
        item["liked_by_me"] = r["id"] in liked
        item["comments"] = by_fb.get(r["id"], [])
        out.append(item)
    return out


def delete(user: dict | None, feedback_id: int) -> dict:
    _ensure_tables()
    uid, _ = _user_label(user)
    if not uid:
        raise ValueError("login required")
    with store._conn() as conn:  # noqa: SLF001
        row = conn.execute("SELECT user_id FROM feedbacks WHERE id=?", (feedback_id,)).fetchone()
        if not row or row["user_id"] != uid:
            raise ValueError("not your feedback")
        conn.execute("UPDATE feedbacks SET status='deleted', updated_time=? WHERE id=?", (_now(), feedback_id))
        return {"id": feedback_id}


def toggle_like(user: dict | None, feedback_id: int) -> dict:
    _ensure_tables()
    uid, _ = _user_label(user)
    if not uid:
        raise ValueError("login required")
    with store._conn() as conn:  # noqa: SLF001
        exists = conn.execute(
            "SELECT 1 FROM feedback_likes WHERE user_id=? AND feedback_id=?", (uid, feedback_id)
        ).fetchone()
        if exists:
            conn.execute("DELETE FROM feedback_likes WHERE user_id=? AND feedback_id=?", (uid, feedback_id))
            conn.execute("UPDATE feedbacks SET likes=likes-1 WHERE id=? AND likes>0", (feedback_id,))
            liked = False
        else:
            conn.execute("INSERT INTO feedback_likes VALUES(?,?)", (uid, feedback_id))
            conn.execute("UPDATE feedbacks SET likes=likes+1 WHERE id=?", (feedback_id,))
            liked = True
        row = conn.execute("SELECT likes FROM feedbacks WHERE id=?", (feedback_id,)).fetchone()
        return {"likes": row["likes"] if row else 0, "liked": liked}


def add_comment(user: dict | None, feedback_id: int, content: str) -> dict:
    _ensure_tables()
    content = (content or "").strip()
    if not content:
        raise ValueError("content required")
    if len(content) > 500:
        raise ValueError("content too long")
    uid, uname = _user_label(user)
    if not uid:
        raise ValueError("login required")
    with store._conn() as conn:  # noqa: SLF001
        cur = conn.execute(
            "INSERT INTO feedback_comments(feedback_id, user_id, username, content, created_time) VALUES(?,?,?,?,?)",
            (feedback_id, uid, uname, content, _now()),
        )
        return {"id": int(cur.lastrowid)}


def toggle_comment_like(user: dict | None, comment_id: int) -> dict:
    _ensure_tables()
    uid, _ = _user_label(user)
    if not uid:
        raise ValueError("login required")
    with store._conn() as conn:  # noqa: SLF001
        exists = conn.execute(
            "SELECT 1 FROM feedback_comment_likes WHERE user_id=? AND comment_id=?", (uid, comment_id)
        ).fetchone()
        if exists:
            conn.execute("DELETE FROM feedback_comment_likes WHERE user_id=? AND comment_id=?", (uid, comment_id))
            conn.execute("UPDATE feedback_comments SET likes=likes-1 WHERE id=? AND likes>0", (comment_id,))
            liked = False
        else:
            conn.execute("INSERT INTO feedback_comment_likes VALUES(?,?)", (uid, comment_id))
            conn.execute("UPDATE feedback_comments SET likes=likes+1 WHERE id=?", (comment_id,))
            liked = True
        row = conn.execute("SELECT likes FROM feedback_comments WHERE id=?", (comment_id,)).fetchone()
        return {"likes": row["likes"] if row else 0, "liked": liked}


# ---- 荣誉殿堂（捐赠墙画廊） ----

def hall_list(page: int = 1, page_size: int = 24) -> dict:
    _ensure_tables()
    with store._conn() as conn:  # noqa: SLF001
        total = conn.execute("SELECT COUNT(*) c FROM hall_of_fame_images WHERE status='published'").fetchone()["c"]
        rows = conn.execute(
            "SELECT * FROM hall_of_fame_images WHERE status='published' ORDER BY sort_order DESC, id DESC LIMIT ? OFFSET ?",
            (page_size, max(page - 1, 0) * page_size),
        ).fetchall()
    return {"list": [dict(r) for r in rows], "total": total}


def hall_upload(admin_user: dict, image_path: str, description: str = "", created_time: str | None = None) -> dict:
    _ensure_tables()
    if (admin_user.get("role") or "user") != "admin":
        raise PermissionError("admin only")
    with store._conn() as conn:  # noqa: SLF001
        cur = conn.execute(
            "INSERT INTO hall_of_fame_images(image_path, description, created_time) VALUES(?,?,?)",
            (image_path, description, created_time or _now()),
        )
        return {"id": int(cur.lastrowid)}


def hall_delete(admin_user: dict, image_id: int) -> dict:
    _ensure_tables()
    if (admin_user.get("role") or "user") != "admin":
        raise PermissionError("admin only")
    with store._conn() as conn:  # noqa: SLF001
        conn.execute("UPDATE hall_of_fame_images SET status='deleted' WHERE id=?", (image_id,))
        return {"id": image_id}
