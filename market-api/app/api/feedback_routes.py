"""意见反馈与荣誉殿堂路由，协议对齐原站 /feedback/* 与 /hall_of_fame/*。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, File, Form, UploadFile
from pydantic import BaseModel

from app.core import deps
from app.core.errors import fail, ok, PARAM_ERROR
from app.services import feedback


router = APIRouter(tags=["feedback"])


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


async def _save_image(file: UploadFile, prefix: str) -> str:
    """校验并保存图片到 backend/uploads/，返回 /uploads/xxx 相对 URL。"""
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise ValueError("仅支持 jpg/png/gif/webp 图片")
    data = await file.read()
    if not data:
        raise ValueError("文件为空")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("图片不能超过 5MB")
    if not _is_valid_image(data, file.content_type):
        raise ValueError("文件内容与图片格式不符")
    import secrets as _secrets
    from pathlib import Path as _Path
    ext = ALLOWED_IMAGE_TYPES[file.content_type]
    name = prefix + _secrets.token_hex(12) + ext
    uploads_dir = _Path(__file__).resolve().parents[2] / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    (uploads_dir / name).write_bytes(data)
    return "/uploads/" + name


class FeedbackSubmitReq(BaseModel):
    content: str
    images: list[str] | None = None


class FeedbackDeleteReq(BaseModel):
    feedback_id: int


class FeedbackLikeReq(BaseModel):
    feedback_id: int


class FeedbackCommentReq(BaseModel):
    feedback_id: int
    content: str


class CommentLikeReq(BaseModel):
    comment_id: int


class HallUploadReq(BaseModel):
    image_path: str
    description: str = ""


class HallDeleteReq(BaseModel):
    image_id: int


@router.get("/feedback/list")
async def feedback_list(page: int = 1, limit: int = 10, user: dict | None = Depends(deps.optional_user)):
    viewer = str(user["id"]) if user else ""
    return ok(feedback.list_feedbacks(page, limit, viewer))


@router.post("/feedback/submit")
async def feedback_submit(req: FeedbackSubmitReq, user: dict | None = Depends(deps.optional_user)):
    try:
        result = feedback.submit(user, req.content, req.images)
    except ValueError as exc:
        return fail(PARAM_ERROR, message=str(exc))
    return ok(result)


@router.post("/feedback/upload")
async def feedback_upload(file: UploadFile = File(...), user: dict | None = Depends(deps.optional_user)):
    """反馈截图上传（原站契约：multipart file 字段，返回 {url}）。未登录也允许，提交时图片随内容入库。"""
    try:
        url = await _save_image(file, "fb_")
    except ValueError as e:
        return fail(PARAM_ERROR, message=str(e))
    return ok({"url": url})


@router.post("/feedback/delete")
async def feedback_delete(req: FeedbackDeleteReq, user: dict = Depends(deps.current_user)):
    try:
        return ok(feedback.delete(user, req.feedback_id))
    except ValueError as exc:
        return fail(PARAM_ERROR, message=str(exc))


@router.post("/feedback/like")
async def feedback_like(req: FeedbackLikeReq, user: dict = Depends(deps.current_user)):
    try:
        return ok(feedback.toggle_like(user, req.feedback_id))
    except ValueError as exc:
        return fail(PARAM_ERROR, message=str(exc))


@router.post("/feedback/comment")
async def feedback_comment(req: FeedbackCommentReq, user: dict = Depends(deps.current_user)):
    try:
        return ok(feedback.add_comment(user, req.feedback_id, req.content))
    except ValueError as exc:
        return fail(PARAM_ERROR, message=str(exc))


@router.post("/feedback/comment/like")
async def feedback_comment_like(req: CommentLikeReq, user: dict = Depends(deps.current_user)):
    try:
        return ok(feedback.toggle_comment_like(user, req.comment_id))
    except ValueError as exc:
        return fail(PARAM_ERROR, message=str(exc))


# ---- 荣誉殿堂 ----

@router.get("/hall_of_fame/list")
async def hall_list(page: int = 1, page_size: int = 24):
    return ok(feedback.hall_list(page, page_size))


@router.post("/hall_of_fame/upload")
async def hall_upload(
    file: UploadFile = File(...),
    description: str = Form(default=""),
    created_time: str = Form(default=""),
    user: dict = Depends(deps.current_user),
):
    """荣誉殿堂上传（原站契约：multipart file/description/created_time）。

    原站返回 code=20000；非管理员 403 由前端隐藏入口兜底。
    """
    if (user.get("role") or "user") != "admin":
        return fail(PARAM_ERROR, message="仅管理员可上传")
    try:
        url = await _save_image(file, "hall_")
    except ValueError as e:
        return fail(PARAM_ERROR, message=str(e))
    try:
        result = feedback.hall_upload(user, url, description, created_time or None)
    except PermissionError:
        return fail(PARAM_ERROR, message="仅管理员可上传")
    result["url"] = url
    return ok(result)


@router.post("/hall_of_fame/delete/{image_id}")
async def hall_delete(image_id: int, user: dict = Depends(deps.current_user)):
    try:
        return ok(feedback.hall_delete(user, image_id))
    except PermissionError:
        return fail(PARAM_ERROR, message="仅管理员可删除")
