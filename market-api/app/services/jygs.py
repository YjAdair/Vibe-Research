# -*- coding: utf-8 -*-
"""韭研公社文章解析：原站 /v3/topic/jygs/parse。"""
from __future__ import annotations

import html as htmlmod
import json
import re

from app.datasources.http import fetch_text

PAGE_BASE = "https://www.jiuyangongshe.com/a/"
NL = chr(10)


def _article_url(url_or_id: str) -> str | None:
    s = (url_or_id or "").strip()
    if not s:
        return None
    if s.startswith("http") and "jiuyangongshe.com" in s:
        return s
    if re.fullmatch(r"[0-9A-Za-z]+", s) and not s.startswith("/"):
        return PAGE_BASE + s
    if s.startswith("/a/"):
        return "https://www.jiuyangongshe.com" + s
    return None


def _extract_str(src: str, field: str) -> str | None:
    BS = chr(92)
    pat = field + ':"' + r'((?:[^"' + BS + BS + r']|' + BS + BS + r'.)*)' + '"'
    m = re.search(pat, src)
    if not m:
        return None
    try:
        return json.loads('"' + m.group(1) + '"')
    except Exception:
        return None


def _html_to_text(h: str) -> str:
    h = re.sub(r"<br" + chr(92) + r"s*/?>", NL, h)
    h = re.sub(r"</p>", NL, h)
    h = re.sub(r"</li>", NL, h)
    h = re.sub(r"<[^>]+>", "", h)
    h = htmlmod.unescape(h)
    h = re.sub(r"[ " + chr(9) + r"]+" + NL, NL, h)
    h = re.sub(NL + "{3,}", NL + NL, h)
    return h.strip()


def _extract_images(content_html: str, cover: str | None) -> list[str]:
    imgs = re.findall(r'<img[^>]+src="([^"]+)"', content_html or "")
    if cover:
        imgs = [cover] + imgs
    seen, out = set(), []
    for u in imgs:
        u = u.split("?")[0]
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


async def parse(url: str) -> dict:
    target = _article_url(url)
    if not target:
        raise ValueError("请输入有效的韭研公社链接")
    resp = await fetch_text(target)
    if resp.status_code != 200:
        raise ValueError("文章不存在")
    src = resp.text or ""
    if not src:
        raise ValueError("文章抓取失败")
    if '"文章不存在"' in src or "statusCode:500" in src:
        raise ValueError("文章不存在")
    title = _extract_str(src, "title")
    content_html = _extract_str(src, "content")
    if not title and not content_html:
        raise ValueError("文章不存在或未登录可见")
    create_time = _extract_str(src, "create_time")
    update_time = _extract_str(src, "update_time")
    cover = _extract_str(src, "cover")
    article_id = target.rsplit("/", 1)[-1]
    content = _html_to_text(content_html or "")
    images = _extract_images(content_html or "", cover)
    return {
        "article_id": article_id,
        "title": title or "",
        "content": content,
        "source": "韭研公社",
        "create_time": create_time or "",
        "update_time": update_time or "",
        "images": images,
        "image_url": images[0] if images else "",
    }
