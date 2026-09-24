"""社区/用户主页服务：帖子、评论、点赞、关注（对齐原站接口形状）。"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime
from typing import Any

from app.core.store import store


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_tables() -> None:
    with store._conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                title TEXT DEFAULT '',
                content TEXT DEFAULT '',
                images TEXT DEFAULT '[]',
                likes INTEGER DEFAULT 0,
                dislikes INTEGER DEFAULT 0,
                status TEXT DEFAULT 'published',
                created_time TEXT,
                updated_time TEXT
            );
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                content TEXT DEFAULT '',
                likes INTEGER DEFAULT 0,
                dislikes INTEGER DEFAULT 0,
                status TEXT DEFAULT 'published',
                created_time TEXT,
                updated_time TEXT
            );
            CREATE TABLE IF NOT EXISTS post_likes (
                user_id TEXT NOT NULL,
                post_id INTEGER NOT NULL,
                PRIMARY KEY (user_id, post_id)
            );
            CREATE TABLE IF NOT EXISTS comment_likes (
                user_id TEXT NOT NULL,
                comment_id INTEGER NOT NULL,
                kind TEXT DEFAULT 'like',
                PRIMARY KEY (user_id, comment_id)
            );
            CREATE TABLE IF NOT EXISTS user_follows (
                follower_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                created_time TEXT,
                PRIMARY KEY (follower_id, target_id)
            );
            CREATE INDEX IF NOT EXISTS idx_posts_user ON posts(user_id, status, created_time DESC);
            CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id, status, created_time ASC);
            """
        )


_ensure_tables()


def _ensure_columns() -> None:
    """已存在的库补列（幂等）。"""
    with store._conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(posts)").fetchall()}
        if "images" not in cols:
            conn.execute("ALTER TABLE posts ADD COLUMN images TEXT DEFAULT '[]'")


def _norm_images(images) -> list:
    """规范化 images：接受 list/JSON 字符串，过滤非法项，限 9 张。"""
    if isinstance(images, str):
        try:
            images = json.loads(images) if images else []
        except (ValueError, TypeError):
            images = []
    if not isinstance(images, list):
        return []
    out = []
    for u in images:
        if not isinstance(u, str):
            continue
        u = u.strip()
        if u and len(u) <= 500 and u not in out:
            out.append(u)
    return out[:9]


_ensure_columns()


def _strip_script(html: str) -> str:
    import re
    return re.sub(r"<script[\s\S]*?>[\s\S]*?</script>", "", html or "", flags=re.I)


def _user_brief(uid: str) -> dict:
    from app.services import user_service
    row = user_service._get_user(uid)
    avatar = ""
    if row:
        try:
            keys = row.keys() if hasattr(row, "keys") else []
            avatar = row["avatar_url"] if "avatar_url" in keys else ""
        except Exception:
            avatar = ""
    return {
        "id": uid,
        "user_nick": row["user_nick"] if row else "",
        "user_name": row["user_nick"] if row else "",
        "avatar_url": avatar,
        "bio": "",
        "followers": follower_count(uid),
    }


def _post_view(row: sqlite3.Row | dict, uid: str | None = None) -> dict:
    item = dict(row)
    item["author_nick"] = (item.get("author_nick") or item.get("user_nick") or "")
    item["username"] = item["author_nick"]
    item["author_avatar_url"] = item.get("author_avatar_url") or ""
    item["liked_by_me"] = bool(uid and liked_post(uid, item["id"]))
    item["images"] = _norm_images(item.get("images"))
    return item


def create_post(uid: str, title: str, content: str, images=None) -> dict:
    title = (title or "").strip()
    content = _strip_script(content or "").strip()
    imgs = _norm_images(images)
    if not title and not content:
        raise ValueError("标题和内容不能同时为空")
    now = _now()
    with store._conn() as conn:
        cur = conn.execute(
            "INSERT INTO posts(user_id, title, content, images, created_time, updated_time) VALUES(?,?,?,?,?,?)",
            (uid, title, content, json.dumps(imgs, ensure_ascii=False), now, now),
        )
        pid = cur.lastrowid
    return {"id": pid, "status": "ok"}


def update_post(uid: str, post_id: int, title: str, content: str, images=None) -> dict:
    with store._conn() as conn:
        row = conn.execute("SELECT id FROM posts WHERE id=? AND user_id=?", (post_id, uid)).fetchone()
        if not row:
            raise ValueError("帖子不存在或无权操作")
        conn.execute(
            "UPDATE posts SET title=?, content=?, images=?, updated_time=? WHERE id=?",
            ((title or "").strip(), _strip_script(content or ""),
             json.dumps(_norm_images(images), ensure_ascii=False), _now(), post_id),
        )
    return {"id": post_id, "status": "ok"}


def delete_post(uid: str, post_id: int) -> dict:
    with store._conn() as conn:
        row = conn.execute("SELECT id FROM posts WHERE id=? AND user_id=?", (post_id, uid)).fetchone()
        if not row:
            raise ValueError("帖子不存在或无权操作")
        conn.execute("UPDATE posts SET status='deleted', updated_time=? WHERE id=?", (_now(), post_id))
    return {"id": post_id, "status": "ok"}


def _list_posts(where: str, params: tuple, uid: str | None, page: int = 1, page_size: int = 8) -> dict:
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 8), 50))
    total = 0
    items: list[dict] = []
    with store._conn() as conn:
        row = conn.execute(f"SELECT COUNT(*) c FROM posts p WHERE {where}", params).fetchone()
        total = row["c"]
        rows = conn.execute(
            f"SELECT p.*, u.user_nick, u.avatar_url AS author_avatar_url FROM posts p LEFT JOIN users u ON u.id = p.user_id"
            f" WHERE {where} ORDER BY p.created_time DESC, p.id DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        items = [_post_view(r, uid) for r in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size, "has_more": page * page_size < total}


def my_posts(uid: str, page: int = 1, page_size: int = 8) -> dict:
    return _list_posts("p.user_id=? AND p.status='published'", (uid,), uid, page, page_size)


def user_posts(target_uid: str, uid: str | None, page: int = 1, page_size: int = 8) -> dict:
    return _list_posts("p.user_id=? AND p.status='published'", (target_uid,), uid, page, page_size)


def hot_posts(uid: str | None = None, limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit or 50), 100))
    with store._conn() as conn:
        rows = conn.execute(
            "SELECT p.*, u.user_nick, u.avatar_url AS author_avatar_url FROM posts p LEFT JOIN users u ON u.id = p.user_id"
            " WHERE p.status='published' ORDER BY p.likes DESC, p.created_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_post_view(r, uid) for r in rows]


def post_detail(post_id: int, uid: str | None = None) -> dict:
    with store._conn() as conn:
        row = conn.execute(
            "SELECT p.*, u.user_nick, u.avatar_url AS author_avatar_url FROM posts p LEFT JOIN users u ON u.id = p.user_id"
            " WHERE p.id=? AND p.status='published'",
            (post_id,),
        ).fetchone()
    if not row:
        raise ValueError("帖子不存在")
    item = _post_view(row, uid)
    author = _user_brief(item["user_id"])
    item["author_nick"] = author["user_nick"]
    item["username"] = author["user_nick"]
    item["followed_by_me"] = (uid or "") and is_following(uid, item["user_id"])
    return item


def toggle_post_like(uid: str, post_id: int) -> dict:
    with store._conn() as conn:
        if liked_post(uid, post_id):
            conn.execute("DELETE FROM post_likes WHERE user_id=? AND post_id=?", (uid, post_id))
            conn.execute("UPDATE posts SET likes = likes - 1 WHERE id=? AND likes > 0", (post_id,))
            liked = False
        else:
            conn.execute("INSERT OR IGNORE INTO post_likes(user_id, post_id) VALUES(?,?)", (uid, post_id))
            conn.execute("UPDATE posts SET likes = likes + 1 WHERE id=?", (post_id,))
            liked = True
        row = conn.execute("SELECT likes FROM posts WHERE id=?", (post_id,)).fetchone()
    return {"likes": row["likes"] if row else 0, "liked": liked}


def liked_post(uid: str, post_id: int) -> bool:
    with store._conn() as conn:
        row = conn.execute("SELECT 1 FROM post_likes WHERE user_id=? AND post_id=?", (uid, post_id)).fetchone()
    return bool(row)


def add_comment(uid: str, post_id: int, content: str) -> dict:
    content = _strip_script(content or "").strip()
    if not content:
        raise ValueError("评论内容不能为空")
    with store._conn() as conn:
        row = conn.execute("SELECT id FROM posts WHERE id=? AND status='published'", (post_id,)).fetchone()
        if not row:
            raise ValueError("帖子不存在")
        cur = conn.execute(
            "INSERT INTO comments(post_id, user_id, content, created_time, updated_time) VALUES(?,?,?,?,?)",
            (post_id, uid, content, _now(), _now()),
        )
        cid = cur.lastrowid
    return {"id": cid, "status": "ok"}


def update_comment(uid: str, comment_id: int, content: str) -> dict:
    content = _strip_script(content or "").strip()
    if not content:
        raise ValueError("评论内容不能为空")
    with store._conn() as conn:
        row = conn.execute("SELECT id FROM comments WHERE id=? AND user_id=? AND status='published'", (comment_id, uid)).fetchone()
        if not row:
            raise ValueError("评论不存在或无权操作")
        conn.execute("UPDATE comments SET content=?, updated_time=? WHERE id=?", (content, _now(), comment_id))
    return {"id": comment_id, "status": "ok"}


def delete_comment(uid: str, comment_id: int) -> dict:
    with store._conn() as conn:
        row = conn.execute("SELECT id FROM comments WHERE id=? AND user_id=?", (comment_id, uid)).fetchone()
        if not row:
            raise ValueError("评论不存在或无权操作")
        conn.execute("UPDATE comments SET status='deleted', updated_time=? WHERE id=?", (_now(), comment_id))
    return {"id": comment_id, "status": "ok"}


def comment_list(post_id: int, uid: str | None = None) -> list[dict]:
    with store._conn() as conn:
        rows = conn.execute(
            "SELECT c.*, u.user_nick, u.avatar_url AS author_avatar_url FROM comments c LEFT JOIN users u ON u.id = c.user_id"
            " WHERE c.post_id=? AND c.status='published' ORDER BY c.created_time ASC, c.id ASC",
            (post_id,),
        ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["liked_by_me"] = bool(uid and _comment_liked(uid, item["id"], "like"))
        item["disliked_by_me"] = bool(uid and _comment_liked(uid, item["id"], "dislike"))
        item["author_nick"] = item.get("user_nick") or ""
        item["author_avatar_url"] = item.get("author_avatar_url") or ""
        out.append(item)
    return out


def _comment_liked(uid: str, comment_id: int, kind: str) -> bool:
    with store._conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM comment_likes WHERE user_id=? AND comment_id=? AND kind=?",
            (uid, comment_id, kind),
        ).fetchone()
    return bool(row)


def toggle_comment_reaction(uid: str, comment_id: int, kind: str) -> dict:
    """kind: like / dislike（踩会取消赞，原站同款交互）。"""
    if kind not in ("like", "dislike"):
        raise ValueError("无效的反应类型")
    other = "dislike" if kind == "like" else "like"
    col = kind + "s"
    ocol = other + "s"
    with store._conn() as conn:
        row = conn.execute("SELECT id FROM comments WHERE id=? AND status='published'", (comment_id,)).fetchone()
        if not row:
            raise ValueError("评论不存在")
        cur = conn.execute(
            "SELECT kind FROM comment_likes WHERE user_id=? AND comment_id=?", (uid, comment_id)
        ).fetchone()
        current = cur["kind"] if cur else None
        if current == kind:
            conn.execute("DELETE FROM comment_likes WHERE user_id=? AND comment_id=?", (uid, comment_id))
            conn.execute(f"UPDATE comments SET {col} = {col} - 1 WHERE id=? AND {col} > 0", (comment_id,))
            active = False
        else:
            if current == other:
                conn.execute(f"UPDATE comments SET {ocol} = {ocol} - 1 WHERE id=? AND {ocol} > 0", (comment_id,))
            conn.execute(
                "INSERT INTO comment_likes(user_id, comment_id, kind) VALUES(?,?,?)"
                " ON CONFLICT(user_id, comment_id) DO UPDATE SET kind=excluded.kind",
                (uid, comment_id, kind),
            )
            conn.execute(f"UPDATE comments SET {col} = {col} + 1 WHERE id=?", (comment_id,))
            active = True
        row = conn.execute(f"SELECT likes, dislikes FROM comments WHERE id=?", (comment_id,)).fetchone()
    out = {"likes": row["likes"], "dislikes": row["dislikes"]}
    out[kind if kind == "like" else "disliked"] = active
    out["liked" if kind == "like" else "dislike"] = active
    return out


def profile_of(target_uid: str, viewer: str | None = None) -> dict:
    from app.services import user_service
    row = user_service._get_user(target_uid)
    if not row:
        raise ValueError("用户不存在")
    view = user_service._user_view(row)
    view["followings"] = following_count(target_uid)
    view["followers"] = follower_count(target_uid)
    view["followed_by_me"] = bool(viewer and viewer != target_uid and is_following(viewer, target_uid))
    view["bio"] = ""
    return view


def profile_me(uid: str) -> dict:
    return profile_of(uid, uid)


def is_following(follower: str, target: str) -> bool:
    with store._conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM user_follows WHERE follower_id=? AND target_id=?", (follower, target)
        ).fetchone()
    return bool(row)


def follower_count(uid: str) -> int:
    with store._conn() as conn:
        row = conn.execute("SELECT COUNT(*) c FROM user_follows WHERE target_id=?", (uid,)).fetchone()
    return row["c"]


def following_count(uid: str) -> int:
    with store._conn() as conn:
        row = conn.execute("SELECT COUNT(*) c FROM user_follows WHERE follower_id=?", (uid,)).fetchone()
    return row["c"]


def toggle_follow(follower: str, target_id: str) -> dict:
    if follower == target_id:
        raise ValueError("不能关注自己")
    with store._conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE id=?", (target_id,)).fetchone()
        if not row:
            raise ValueError("用户不存在")
        if is_following(follower, target_id):
            conn.execute("DELETE FROM user_follows WHERE follower_id=? AND target_id=?", (follower, target_id))
            followed = False
        else:
            conn.execute(
                "INSERT OR REPLACE INTO user_follows(follower_id, target_id, created_time) VALUES(?,?,?)",
                (follower, target_id, _now()),
            )
            followed = True
    return {"followed": followed, "followers": follower_count(target_id)}


def followings_of(uid: str, viewer: str | None = None) -> list[dict]:
    with store._conn() as conn:
        rows = conn.execute(
            "SELECT u.id, u.user_nick, u.role FROM users u JOIN user_follows f ON f.target_id = u.id"
            " WHERE f.follower_id=? ORDER BY f.created_time DESC",
            (uid,),
        ).fetchall()
    out = []
    for r in rows:
        item = _user_brief(r["id"])
        item["followed_by_me"] = bool(viewer and is_following(viewer, r["id"]))
        out.append(item)
    return out


def followers_of(uid: str, viewer: str | None = None) -> list[dict]:
    with store._conn() as conn:
        rows = conn.execute(
            "SELECT u.id, u.user_nick, u.role FROM users u JOIN user_follows f ON f.follower_id = u.id"
            " WHERE f.target_id=? ORDER BY f.created_time DESC",
            (uid,),
        ).fetchall()
    out = []
    for r in rows:
        item = _user_brief(r["id"])
        item["followed_by_me"] = bool(viewer and is_following(viewer, r["id"]))
        out.append(item)
    return out
