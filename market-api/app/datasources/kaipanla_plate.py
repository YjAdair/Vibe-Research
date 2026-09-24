"""开盘啦精选板块榜。目标站 17 类强度与这里是同一列数。

实时榜不带交易日，调用方传入已核对的会话日。历史榜必须带过去的交易日，
不能拿实时榜回填。采集时间不写成行情时间。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import asyncio
import httpx

URL = "https://apphq.longhuvip.com/w1/api/index.php"
HISTORY_URL = "https://apphis.longhuvip.com/w1/api/index.php"
HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
    # 不带这个标识时接口返回空列表。
    "User-Agent": "lhb/5.17.9 (iPhone; iOS 16.6.0)",
}
TZ = timezone(timedelta(hours=8))


def parse_rank_row(item: list) -> dict | None:
    if not isinstance(item, list) or len(item) < 7:
        return None
    code, name, score, rate, _speed, amount, money = item[:7]
    if not isinstance(code, str) or not code.isdigit() or not isinstance(name, str):
        return None
    if not all(isinstance(v, (int, float)) for v in (score, rate, amount, money)):
        return None
    row = {
        "plate_code": code,
        "plate_name": name,
        "score": score,
        "rate": rate,
        "trade_money": amount,
        "money_leader": money,
        "source": "kaipanla_zhishu_ranking",
        "source_as_of": None,
    }
    if len(item) >= 9 and all(isinstance(v, (int, float)) for v in item[7:9]):
        buy, sell = item[7], item[8]
        if abs((buy + sell) - money) < 1:
            row["money_leader_buy"] = buy
            row["money_leader_sell"] = sell
    return row


async def _fetch_pages(client: httpx.AsyncClient, url: str, day: str | None, page_size: int) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    index = 0
    while index < 400:
        body = {
            "Index": str(index),
            "Order": "1",
            "PhoneOSNew": "2",
            "Type": "1",
            "VerSion": "5.17.0.9",
            "ZSType": "7",
            "a": "RealRankingInfo",
            "apiv": "w38",
            "c": "ZhiShuRanking",
            "st": str(page_size),
        }
        if day:
            body["Date"] = day
        response = await client.post(url, data=body)
        response.raise_for_status()
        payload = response.json()
        if str(payload.get("errcode") or "0") not in ("0", "0.0"):
            raise ValueError(payload.get("errmsg") or "kaipanla plate rank rejected")
        page = payload.get("list") or []
        if not page:
            break
        added = 0
        for item in page:
            row = parse_rank_row(item)
            if row and row["plate_code"] not in seen:
                seen.add(row["plate_code"])
                rows.append(row)
                added += 1
        if added == 0:
            break
        index += len(page)
    return rows


async def fetch_realtime_rank() -> list[dict]:
    async with httpx.AsyncClient(timeout=20, headers=HEADERS) as client:
        rows = await _fetch_pages(client, URL, None, 80)
    if not rows:
        raise ValueError("empty kaipanla plate rank")
    return rows


async def fetch_history_rank(day: str) -> list[dict]:
    """过去交易日的精选板块榜。当天不要走这里，历史主机不收当日。"""
    async with httpx.AsyncClient(timeout=20, headers=HEADERS) as client:
        rows = await _fetch_pages(client, HISTORY_URL, day, 60)
    if len(rows) < 100:
        raise ValueError(f"incomplete kaipanla plate rank {day}: {len(rows)}")
    return rows


async def _publish(day: str, rows: list[dict], status: str = "final") -> dict:
    from app.core.store import store
    from app.services import pools

    collected_at = datetime.now(TZ).isoformat()
    for row in rows:
        row["date1"] = day
        row["plate_type"] = 17
        row["collected_at"] = collected_at
    saved = store.plate_rank_save(17, day, rows)
    exact = store.plate_rank_exact_save(17, day, {r["plate_code"]: float(r["money_leader"]) for r in rows})
    # 题材榜落库后补当日梯队池（全题材共用）；失败不阻断榜发布
    ladder_pools = None
    try:
        ladder_pools = await pools.collect_session(day)
    except Exception as exc:
        ladder_pools = {"status": "missing", "error": type(exc).__name__}
    return {
        "date": day,
        "rows": saved,
        "exact": exact,
        "source": "kaipanla_zhishu_ranking",
        "collected_at": collected_at,
        "status": status,
        "ladder_pools": ladder_pools,
    }


async def fetch_plate_stocks(plate_id: str, day: str, *, page_size: int = 50) -> list[dict]:
    """板块成分股（开盘啦 ZhiShuStockList_W8 全量分页）。

    apphis 历史主机；当天通常还没有。Type=0 + Index 步进分页拉全量
    （芯片 801001@2026-09-21 实测 1130 只），不再用 Type0-19×st=9 切片并集。
    """
    if not plate_id or not str(plate_id).isdigit():
        raise ValueError("invalid plate id")
    if len(day) == 8 and day.isdigit():
        day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    st = max(10, min(int(page_size or 50), 100))
    seen: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=30, headers=HEADERS) as client:
        index = 0
        while index < 5000:
            body = {
                "a": "ZhiShuStockList_W8",
                "c": "ZhiShuRanking",
                "apiv": "w38",
                "PhoneOSNew": "2",
                "VerSion": "5.17.0.9",
                "PlateID": str(plate_id),
                "Date": day,
                "Type": "0",
                "Index": str(index),
                "st": str(st),
                "Order": "1",
            }
            response = await client.post(HISTORY_URL, data=body)
            response.raise_for_status()
            payload = response.json() if response.text else {}
            if str(payload.get("errcode") or "0") not in ("0", "0.0"):
                break
            page = payload.get("list") or []
            if not page:
                break
            added = 0
            for item in page:
                if not isinstance(item, list) or not item:
                    continue
                code = str(item[0] or "")
                if len(code) != 6 or not code.isdigit() or code in seen:
                    continue
                name = str(item[1] or "") if len(item) > 1 else ""
                seen[code] = {
                    "stock_code": code,
                    "stock_name": name,
                    "source": "kaipanla_zhishu_stock_list_w8",
                    "plate_code": str(plate_id),
                    "as_of": day,
                }
                added += 1
            if added == 0 or len(page) < st:
                break
            index += len(page)
            await asyncio.sleep(0.12)
    return list(seen.values())


async def fetch_son_plates(plate_id: str) -> list[dict]:
    """一级题材的二级列表（开盘啦 SonPlate_Info）。返回真实 801/803 子代码。"""
    if not plate_id or not str(plate_id).isdigit():
        raise ValueError("invalid plate id")
    body = {
        "a": "SonPlate_Info",
        "c": "ZhiShuRanking",
        "apiv": "w38",
        "PhoneOSNew": "2",
        "VerSion": "5.17.0.9",
        "PlateID": str(plate_id),
    }
    async with httpx.AsyncClient(timeout=20, headers=HEADERS) as client:
        response = await client.post(URL, data=body)
        response.raise_for_status()
        payload = response.json() if response.text else {}
    if str(payload.get("errcode") or "0") not in ("0", "0.0"):
        raise ValueError(payload.get("errmsg") or "kaipanla son plate rejected")
    out = []
    for item in payload.get("List") or []:
        if not isinstance(item, list) or len(item) < 2:
            continue
        code, name = str(item[0] or ""), str(item[1] or "")
        if code.isdigit() and name:
            out.append({"code": code, "name": name, "source": "kaipanla_son_plate_info"})
    return out


async def fetch_theme_tags(zs_code: str) -> list[dict]:
    """开盘啦题材标签（InfoBKR）。ID 不是 801 二级代码，只作展示标签。"""
    url = "https://applhb.longhuvip.com/w1/api/index.php"
    body = {
        "a": "InfoBKR",
        "c": "Theme",
        "apiv": "w38",
        "PhoneOSNew": "2",
        "VerSion": "5.17.0.9",
        "ZSCode": str(zs_code),
    }
    async with httpx.AsyncClient(timeout=20, headers=HEADERS) as client:
        response = await client.post(url, data=body)
        response.raise_for_status()
        payload = response.json() if response.text else {}
    out = []
    for row in payload.get("List") or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("ID") or "").strip()
        name = str(row.get("Name") or "").strip()
        if code and name:
            out.append({"code": f"kpl_{code}", "name": name, "source": "kaipanla_theme_infobkr"})
    return out


async def publish_realtime_rank(day: str, status: str = "partial_preview") -> dict:
    """把实时榜写入 plate_rank_daily。day 必须是调用方已核对的交易日。"""
    return await _publish(day, await fetch_realtime_rank(), status)


async def publish_history_rank(day: str, status: str = "final") -> dict:
    return await _publish(day, await fetch_history_rank(day), status)

