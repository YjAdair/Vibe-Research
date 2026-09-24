"""社区路由：帖子/评论/点赞/关注/个人主页（对齐原站 /user/* 路径）。"""

from __future__ import annotations

from fastapi import APIRouter, File, UploadFile, Depends
from pydantic import BaseModel

from app.core.deps import current_user, optional_user
from app.core.errors import ok, fail, PARAM_ERROR
from app.services import community

router = APIRouter(prefix="/v3")


class PostCreateBody(BaseModel):
    title: str = ""
    content: str = ""
    images: list = []


class PostUpdateBody(PostCreateBody):
    post_id: int


class PostDeleteBody(BaseModel):
    post_id: int


class PostLikeBody(BaseModel):
    post_id: int


class CommentCreateBody(BaseModel):
    post_id: int
    content: str


class CommentUpdateBody(BaseModel):
    comment_id: int
    content: str


class CommentDeleteBody(BaseModel):
    comment_id: int


class CommentLikeBody(BaseModel):
    comment_id: int


class FollowToggleBody(BaseModel):
    target_user_id: str


@router.get("/user/info")
async def user_info(user: dict = Depends(current_user)):
    return ok(user)


@router.get("/user/profile/me")
async def profile_me(user: dict = Depends(current_user)):
    return ok(community.profile_me(user["id"]))


@router.get("/user/profile/{target_id}")
async def profile_of(target_id: str, viewer: dict | None = Depends(optional_user)):
    return ok(community.profile_of(target_id, viewer["id"] if viewer else None))


@router.post("/user/post/create")
async def post_create(body: PostCreateBody, user: dict = Depends(current_user)):
    return ok(community.create_post(user["id"], body.title, body.content, body.images))


@router.get("/user/post/my")
async def my_posts(page: int = 1, page_size: int = 8, user: dict = Depends(current_user)):
    return ok(community.my_posts(user["id"], page, page_size))


@router.get("/user/post/user/{target_id}")
async def user_posts(target_id: str, page: int = 1, page_size: int = 8, viewer: dict | None = Depends(optional_user)):
    return ok(community.user_posts(target_id, viewer["id"] if viewer else None, page, page_size))


@router.get("/user/post/hot")
async def hot_posts(limit: int = 50, viewer: dict | None = Depends(optional_user)):
    return ok(community.hot_posts(viewer["id"] if viewer else None, limit))


@router.get("/user/post/detail/{post_id}")
async def post_detail(post_id: int, viewer: dict | None = Depends(optional_user)):
    try:
        return ok(community.post_detail(post_id, viewer["id"] if viewer else None))
    except ValueError as e:
        return fail(40404, str(e))


@router.post("/user/post/update")
async def post_update(body: PostUpdateBody, user: dict = Depends(current_user)):
    return ok(community.update_post(user["id"], body.post_id, body.title, body.content, body.images))


ALLOWED_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _is_valid_image(data: bytes, content_type: str) -> bool:
    """魔数校验：防止伪造 Content-Type 上传非图片内容。"""
    if content_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if content_type == "image/webp":
        return len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


@router.post("/user/post/image/upload")
async def post_image_upload(file: UploadFile = File(...), user: dict = Depends(current_user)):
    """帖子图片上传（原站契约：multipart file 字段，返回 {url}）。

    本地磁盘存储（backend/uploads/，静态挂载 /uploads）；
    商业化部署时可替换为对象存储，仅此一处需要改动。
    """
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        return fail(PARAM_ERROR, message="仅支持 jpg/png/gif/webp 图片")
    data = await file.read()
    if not data:
        return fail(PARAM_ERROR, message="文件为空")
    if len(data) > MAX_IMAGE_BYTES:
        return fail(PARAM_ERROR, message="图片不能超过 5MB")
    if not _is_valid_image(data, file.content_type):
        return fail(PARAM_ERROR, message="文件内容与图片格式不符")
    import secrets as _secrets
    from pathlib import Path as _Path
    ext = ALLOWED_IMAGE_TYPES[file.content_type]
    name = _secrets.token_hex(12) + ext
    uploads_dir = _Path(__file__).resolve().parents[2] / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    (uploads_dir / name).write_bytes(data)
    return ok({"url": "/uploads/" + name})


@router.post("/user/post/delete")
async def post_delete(body: PostDeleteBody, user: dict = Depends(current_user)):
    return ok(community.delete_post(user["id"], body.post_id))


@router.post("/user/post/like")
async def post_like(body: PostLikeBody, user: dict = Depends(current_user)):
    return ok(community.toggle_post_like(user["id"], body.post_id))


@router.post("/user/post/comment")
async def comment_create(body: CommentCreateBody, user: dict = Depends(current_user)):
    return ok(community.add_comment(user["id"], body.post_id, body.content))


@router.get("/user/post/comment/list/{post_id}")
async def comment_list(post_id: int, viewer: dict | None = Depends(optional_user)):
    return ok(community.comment_list(post_id, viewer["id"] if viewer else None))


@router.post("/user/post/comment/update")
async def comment_update(body: CommentUpdateBody, user: dict = Depends(current_user)):
    return ok(community.update_comment(user["id"], body.comment_id, body.content))


@router.post("/user/post/comment/delete")
async def comment_delete(body: CommentDeleteBody, user: dict = Depends(current_user)):
    return ok(community.delete_comment(user["id"], body.comment_id))


@router.post("/user/post/comment/like")
async def comment_like(body: CommentLikeBody, user: dict = Depends(current_user)):
    return ok(community.toggle_comment_reaction(user["id"], body.comment_id, "like"))


@router.post("/user/post/comment/dislike")
async def comment_dislike(body: CommentLikeBody, user: dict = Depends(current_user)):
    return ok(community.toggle_comment_reaction(user["id"], body.comment_id, "dislike"))


@router.post("/user/follow/toggle")
async def follow_toggle(body: FollowToggleBody, user: dict = Depends(current_user)):
    return ok(community.toggle_follow(user["id"], body.target_user_id))


@router.get("/user/followings/{target_id}")
async def followings(target_id: str, viewer: dict | None = Depends(optional_user)):
    return ok(community.followings_of(target_id, viewer["id"] if viewer else None))


@router.get("/user/followers/{target_id}")
async def followers(target_id: str, viewer: dict | None = Depends(optional_user)):
    return ok(community.followers_of(target_id, viewer["id"] if viewer else None))


@router.get("/user/fans/{target_id}")
async def fans_alias(target_id: str, viewer: dict | None = Depends(optional_user)):
    return ok(community.followers_of(target_id, viewer["id"] if viewer else None))


@router.get("/user/fans/list/{target_id}")
async def fans_list_alias(target_id: str, viewer: dict | None = Depends(optional_user)):
    return ok(community.followers_of(target_id, viewer["id"] if viewer else None))


@router.get("/user/follower/list/{target_id}")
async def follower_list_alias(target_id: str, viewer: dict | None = Depends(optional_user)):
    return ok(community.followers_of(target_id, viewer["id"] if viewer else None))
