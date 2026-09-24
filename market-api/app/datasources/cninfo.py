"""巨潮资讯公告：交易所严重异常波动披露（免费）。"""
from __future__ import annotations
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import asyncio
import httpx
from app.datasources.http import fetch_form

TZ = ZoneInfo('Asia/Shanghai')
URL = 'http://www.cninfo.com.cn/new/hisAnnouncement/query'
HEADERS = {
    'Referer': 'http://www.cninfo.com.cn/',
    'Origin': 'http://www.cninfo.com.cn',
    'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
}
TAG = re.compile(r'<[^>]+>')
SEVERE = re.compile(r'严重异常波动')


def notice_date(ms) -> str:
    stamp = int(ms)
    if stamp > 10**12:
        stamp //= 1000
    return datetime.fromtimestamp(stamp, TZ).date().isoformat()


def clean_title(title: str) -> str:
    return TAG.sub('', title or '').replace(' ', '')


def parse_page(data: dict) -> tuple[int, list[dict]]:
    rows = data.get('announcements')
    if not isinstance(rows, list):
        raise ValueError('Invalid cninfo announcement page')
    total = data.get('totalAnnouncement')
    if total is None:
        total = data.get('totalRecord')
    if total is None:
        raise ValueError('cninfo page missing total')
    items = []
    seen = set()
    for row in rows:
        code = str(row.get('secCode') or '').zfill(6)
        title = clean_title(row.get('announcementTitle') or '')
        if len(code) != 6 or not code.isdigit() or not SEVERE.search(title):
            continue
        day = notice_date(row.get('announcementTime'))
        key = (code, day, title)
        if key in seen:
            continue
        seen.add(key)
        items.append({
            'stock_code': code,
            'stock_name': row.get('secName') or '',
            'notice_date': day,
            'title': title,
            'announcement_time': int(row.get('announcementTime') or 0),
            'source': 'cninfo',
        })
    return int(total), items


async def _post(payload: dict) -> dict:
    last = None
    for attempt in range(4):
        try:
            return await fetch_form(URL, payload, headers=HEADERS)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
            await asyncio.sleep(0.6 * (attempt + 1))
    raise last


async def severe_notices(start: str, end: str, page_size: int = 20) -> list[dict]:
    """Paginate 严重异常波动 announcements. Incomplete totals fail the batch."""
    if start > end:
        raise ValueError('Invalid announcement date range')
    first = await _post({
        'pageNum': 1, 'pageSize': page_size, 'column': 'szse', 'tabName': 'fulltext',
        'plate': '', 'stock': '', 'searchkey': '严重异常波动', 'secid': '',
        'sdate': start, 'edate': end, 'isHLtitle': 'true', 'category': '', 'trade': '',
    })
    total, items = parse_page(first)
    pages = max(1, (total + page_size - 1) // page_size)
    for page in range(2, pages + 1):
        nxt = await _post({
            'pageNum': page, 'pageSize': page_size, 'column': 'szse', 'tabName': 'fulltext',
            'plate': '', 'stock': '', 'searchkey': '严重异常波动', 'secid': '',
            'sdate': start, 'edate': end, 'isHLtitle': 'true', 'category': '', 'trade': '',
        })
        ntotal, chunk = parse_page(nxt)
        if ntotal != total:
            raise ValueError('cninfo total changed during pagination')
        if not chunk and page <= pages:
            raise ValueError('cninfo truncated announcement page')
        items.extend(chunk)
    unique = {}
    for item in items:
        unique[(item['stock_code'], item['notice_date'], item['title'])] = item
    return sorted(unique.values(), key=lambda r: (r['notice_date'], r['stock_code']))
