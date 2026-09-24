# -*- coding: utf-8 -*-
"""表格图片 OCR：优先通义 qwen-vl（原站同源），未配置时返回明确错误。"""
from __future__ import annotations

import base64
import json
import os

import httpx

DASHSCOPE_BASE = "https://dashscope.aliyuncs.com"
VL_MODEL = "qwen-vl-max"
TEXT_MODEL = "qwen-plus"


def has_api_key() -> bool:
    return bool(os.environ.get("DASHSCOPE_API_KEY"))


def _headers() -> dict:
    return {"Authorization": "Bearer " + os.environ["DASHSCOPE_API_KEY"], "Content-Type": "application/json"}


async def _generate(model: str, messages: list[dict], timeout: float = 120.0) -> str:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            DASHSCOPE_BASE + "/api/v1/services/aigc/multimodal-generation/generation",
            headers=_headers(),
            json={"model": model, "input": {"messages": messages}},
        )
        data = r.json()
    if r.status_code != 200:
        raise ValueError((data.get("message") or {}).get("text_content") or "AI 服务调用失败")
    try:
        return data["output"]["choices"][0]["message"]["content"][0]["text"]
    except Exception:
        raise ValueError("AI 返回格式异常")


def _parse_rows(text: str) -> list[list[str]]:
    """从模型输出提取表格行：优先 JSON，回退按竖线/制表符切分。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("```")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, list):
                rows = []
                for it in obj:
                    if isinstance(it, list):
                        rows.append([str(c).strip() for c in it])
                    elif isinstance(it, dict):
                        rows.append([str(v).strip() for v in it.values()])
                return rows
        except Exception:
            pass
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or set(line) <= {"|", "-", " ", "+"}:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells:
            rows.append(cells)
    return rows


async def table_ocr(image_bytes: bytes | None = None, image_url: str | None = None, engine: str = "qwen") -> list[list[str]]:
    if not has_api_key():
        raise ValueError("未配置 DASHSCOPE_API_KEY，表格图片识别功能不可用；请配置通义千问 API Key")
    if not image_bytes and not image_url:
        raise ValueError("请上传图片或提供图片链接")
    if image_bytes:
        b64 = base64.b64encode(image_bytes).decode()
        img = "data:image/jpeg;base64," + b64
    else:
        img = image_url
    prompt = (
        "你是表格识别助手。请识别图片中的表格，严格输出 JSON 数组（数组的数组），"
        "每个内层数组是一行单元格文本，保留原始行列结构，不要输出任何解释文字。"
        "示例：[[\"股票\",\"涨幅\"],[\"贵州茅台\",\"5.2%\"]]"
    )
    content = [{"type": "text", "text": prompt}, {"type": "image", "image": img}]
    text = await _generate(VL_MODEL, [{"role": "user", "content": content}])
    return _parse_rows(text)


async def merge_content(article_id: str, current_content: str) -> str:
    """AI 融合文章内容：原文要点并入当前内容，无重复、保留结构。未配置时降级为追加合并。"""
    current_content = (current_content or "").strip()
    if not article_id or not current_content:
        return current_content
    if not has_api_key():
        return current_content
    try:
        from app.services import jygs
        article = await jygs.parse(article_id)
        article_content = (article.get("content") or "").strip()
    except Exception:
        return current_content
    if not article_content:
        return current_content
    prompt = (
        "请将参考文章中的增量信息融合到当前内容中，去除与当前内容重复的部分，"
        "保持当前内容的结构与措辞风格，只输出融合后的完整文本，不要任何解释。\n"
        "【当前内容】\n" + current_content + "\n\n【参考文章】\n" + article_content
    )
    merged = await _generate(TEXT_MODEL, [{"role": "user", "content": prompt}], timeout=180.0)
    return (merged or "").strip() or current_content
