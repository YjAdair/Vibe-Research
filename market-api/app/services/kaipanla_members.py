"""开盘啦精选板块成分：写入本地 members 快照，供个股/人气/涨幅/梯队只读。

来源 ZhiShuStockList_W8（Type0-19 切片并集），不是全量官方成分；当天 apphis
通常无数据，取最近有回报的历史日。不把原站数据写入产品库。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.store import store
from app.datasources import kaipanla_plate
from app.services import plate_flow

TZ = ZoneInfo("Asia/Shanghai")
MEMBERS_PATH = plate_flow.MEMBERS_PATH
SOURCE = "kaipanla_zhishu_stock_list_w8_fullpage"



def _top_plate_codes(days: int = 15, top_n: int = 30) -> list[str]:
    dates = store.plate_rank_dates(17, days)
    seen: set[str] = set()
    ordered: list[str] = []
    for day in dates:
        rows = store.plate_rank_range(17, day, day)
        ranked = sorted(
            (r for r in rows if r.get("plate_code")),
            key=lambda r: (-(r.get("score") if isinstance(r.get("score"), (int, float)) else -1e18), r["plate_code"]),
        )[:top_n]
        for row in ranked:
            code = str(row["plate_code"])
            if code not in seen:
                seen.add(code)
                ordered.append(code)
    return ordered


def _plate_name(code: str) -> str:
    for day in store.plate_rank_dates(17, 5):
        rows = store.plate_rank_range(17, day, day)
        for row in rows:
            if str(row.get("plate_code")) == code and row.get("plate_name"):
                return str(row["plate_name"])
    return code


def _load_snapshot() -> dict:
    if MEMBERS_PATH.exists():
        try:
            return json.loads(MEMBERS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {"meta": {}, "plates": {}, "stock_names": {}}


def _save_snapshot(snapshot: dict) -> None:
    MEMBERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MEMBERS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    tmp.replace(MEMBERS_PATH)
    plate_flow._members_cache = None  # noqa: SLF001
    plate_flow._members_mtime = 0.0  # noqa: SLF001


def _needs_son_refill(entry: dict, plate_code: str) -> bool:
    sons = entry.get("sub_plates") or []
    if not sons:
        return True
    if len(sons) == 1 and str(sons[0].get("code")) == str(plate_code):
        return True
    return False


async def _resolve_member_day(end: str, lookback: int = 8) -> str | None:
    """找 end 及之前最近一个对样本板块有成分回报的交易日。"""
    dates = [d for d in store.plate_rank_dates(17, lookback + 5) if d <= end]
    sample = "803023"
    for day in dates:
        rows = await kaipanla_plate.fetch_plate_stocks(sample, day)
        if rows:
            return day
    return None


async def collect_top_members(end: str | None = None, days: int = 15, top_n: int = 30,
                              concurrency: int = 1, only_missing: bool = False) -> dict:
    """采集题材的二级目录 + 各二级成分（切片并集）+ 一级并集。"""
    end = end or datetime.now(TZ).date().isoformat()
    member_day = await _resolve_member_day(end)
    if not member_day:
        raise ValueError("no kaipanla plate stock day available")
    snapshot = _load_snapshot()
    bucket = snapshot.setdefault("plates", {})
    names = snapshot.setdefault("stock_names", {})
    if only_missing:
        plates = [c for c, e in bucket.items() if _needs_son_refill(e, c)]
        # 榜上新题材也补
        for c in _top_plate_codes(days, top_n):
            if c not in bucket:
                plates.append(c)
    else:
        plates = _top_plate_codes(days, top_n)
        for c in list(bucket):
            if c not in plates and _needs_son_refill(bucket[c], c):
                plates.append(c)
    sem = asyncio.Semaphore(concurrency)
    saved, errors, all_codes = 0, [], set()

    async def one(code: str) -> None:
        nonlocal saved
        async with sem:
            try:
                sons = await kaipanla_plate.fetch_son_plates(code)
                await asyncio.sleep(0.2)
                day_stocks: dict[str, list[str]] = {}
                for son in sons:
                    rows = await kaipanla_plate.fetch_plate_stocks(son["code"], member_day)
                    await asyncio.sleep(0.2)
                    day_stocks[son["code"]] = [r["stock_code"] for r in rows]
                    for r in rows:
                        if r.get("stock_name"):
                            names[r["stock_code"]] = r["stock_name"]
                    all_codes.update(day_stocks[son["code"]])
                if not sons:
                    rows = await kaipanla_plate.fetch_plate_stocks(code, member_day)
                    day_stocks[code] = [r["stock_code"] for r in rows]
                    for r in rows:
                        if r.get("stock_name"):
                            names[r["stock_code"]] = r["stock_name"]
                    all_codes.update(day_stocks[code])
                    sons = [{"code": code, "name": _plate_name(code)}]
                else:
                    parent_rows = await kaipanla_plate.fetch_plate_stocks(code, member_day)
                    await asyncio.sleep(0.15)
                    parent_codes = [r["stock_code"] for r in parent_rows]
                    for r in parent_rows:
                        if r.get("stock_name"):
                            names[r["stock_code"]] = r["stock_name"]
                    union = sorted(set(parent_codes) | {c for arr in day_stocks.values() for c in arr})
                    day_stocks[code] = union
                    all_codes.update(union)
            except Exception as exc:  # noqa: BLE001
                errors.append({"plate_code": code, "error": type(exc).__name__})
                return
        if not any(day_stocks.values()):
            errors.append({"plate_code": code, "error": "empty"})
            return
        name = _plate_name(code)
        entry = bucket.setdefault(code, {"plate_name": name, "sub_plates": [], "stocks": {}})
        entry["plate_name"] = name
        entry["sub_plates"] = [{"code": s["code"], "name": s["name"]} for s in sons]
        entry.setdefault("stocks", {})[member_day] = day_stocks
        saved += 1

    await asyncio.gather(*(one(c) for c in plates))
    meta = snapshot.setdefault("meta", {})
    meta.update({
        "source": SOURCE,
        "son_source": "kaipanla_son_plate_info",
        "members_updated_at": member_day,
        "collected_at": datetime.now(TZ).isoformat(),
        "coverage_note": "sons=SonPlate_Info; stocks=W8 Type0 Index full page; parent=full∪sons",
        "plate_count": len(bucket),
        "stock_count": len({c for v in bucket.values() for dm in (v.get("stocks") or {}).values() for arr in (dm or {}).values() for c in (arr or [])}),
        "stock_name_count": len(names),
    })
    _save_snapshot(snapshot)
    return {
        "member_day": member_day,
        "plates": saved,
        "stocks": len(all_codes),
        "names": len(names),
        "errors": errors[:20],
        "error_n": len(errors),
        "path": str(MEMBERS_PATH),
    }


async def collect_plate_members(plate_codes: list[str], end: str | None = None) -> dict:
    """指定板块强制重采全量成分（用于芯片等对拍）。"""
    end = end or datetime.now(TZ).date().isoformat()
    member_day = await _resolve_member_day(end)
    if not member_day:
        raise ValueError("no kaipanla plate stock day available")
    snapshot = _load_snapshot()
    bucket = snapshot.setdefault("plates", {})
    names = snapshot.setdefault("stock_names", {})
    saved, errors, all_codes = 0, [], set()
    for code in plate_codes:
        try:
            sons = await kaipanla_plate.fetch_son_plates(code)
            await asyncio.sleep(0.2)
            day_stocks: dict[str, list[str]] = {}
            for son in sons:
                rows = await kaipanla_plate.fetch_plate_stocks(son["code"], member_day)
                await asyncio.sleep(0.15)
                day_stocks[son["code"]] = [r["stock_code"] for r in rows]
                for r in rows:
                    if r.get("stock_name"):
                        names[r["stock_code"]] = r["stock_name"]
                all_codes.update(day_stocks[son["code"]])
            if not sons:
                rows = await kaipanla_plate.fetch_plate_stocks(code, member_day)
                day_stocks[code] = [r["stock_code"] for r in rows]
                for r in rows:
                    if r.get("stock_name"):
                        names[r["stock_code"]] = r["stock_name"]
                all_codes.update(day_stocks[code])
                sons = [{"code": code, "name": _plate_name(code)}]
            else:
                # 父键也拉一级全量，避免仅子并集漏直属成分
                parent_rows = await kaipanla_plate.fetch_plate_stocks(code, member_day)
                await asyncio.sleep(0.15)
                parent_codes = [r["stock_code"] for r in parent_rows]
                for r in parent_rows:
                    if r.get("stock_name"):
                        names[r["stock_code"]] = r["stock_name"]
                union = sorted(set(parent_codes) | {c for arr in day_stocks.values() for c in arr})
                day_stocks[code] = union
                all_codes.update(union)
        except Exception as exc:  # noqa: BLE001
            errors.append({"plate_code": code, "error": type(exc).__name__})
            continue
        if not any(day_stocks.values()):
            errors.append({"plate_code": code, "error": "empty"})
            continue
        name = _plate_name(code)
        entry = bucket.setdefault(code, {"plate_name": name, "sub_plates": [], "stocks": {}})
        entry["plate_name"] = name
        entry["sub_plates"] = [{"code": s["code"], "name": s["name"]} for s in sons]
        entry.setdefault("stocks", {})[member_day] = day_stocks
        saved += 1
    meta = snapshot.setdefault("meta", {})
    meta.update({
        "source": SOURCE,
        "son_source": "kaipanla_son_plate_info",
        "members_updated_at": member_day,
        "collected_at": datetime.now(TZ).isoformat(),
        "coverage_note": "sons=SonPlate_Info; stocks=W8 Type0 Index full page; parent=full∪sons",
        "plate_count": len(bucket),
        "stock_name_count": len(names),
    })
    _save_snapshot(snapshot)
    return {
        "member_day": member_day,
        "plates": saved,
        "stocks": len(all_codes),
        "names": len(names),
        "errors": errors,
        "path": str(MEMBERS_PATH),
    }


async def backfill_stock_names(concurrency: int = 8) -> dict:
    """腾讯实时补名称 → snapshot.stock_names + daily_close 最近日。"""
    from app.datasources import tencent

    snapshot = _load_snapshot()
    names = snapshot.setdefault("stock_names", {})
    codes: set[str] = set()
    for v in (snapshot.get("plates") or {}).values():
        for day_map in (v.get("stocks") or {}).values():
            for arr in (day_map or {}).values():
                codes.update(str(c) for c in (arr or []) if c)
    missing = sorted(c for c in codes if not names.get(c))
    if not missing:
        return {"stocks": len(codes), "filled": 0, "names": len(names)}
    sem = asyncio.Semaphore(concurrency)
    filled = 0
    errors = 0

    async def batch(chunk: list[str]) -> None:
        nonlocal filled, errors
        async with sem:
            try:
                data = await tencent.realtime(chunk)
            except Exception:
                errors += 1
                return
        for code, q in data.items():
            n = (q or {}).get("name")
            if n:
                names[code] = str(n)
                filled += 1

    chunks = [missing[i:i + 60] for i in range(0, len(missing), 60)]
    await asyncio.gather(*(batch(ch) for ch in chunks))
    _save_snapshot(snapshot)
    patched = 0
    if names:
        with store._conn() as conn:  # noqa: SLF001
            for c, n in names.items():
                cur = conn.execute(
                    "UPDATE daily_close SET stock_name=? WHERE stock_code=? AND (stock_name IS NULL OR stock_name='')",
                    (n, c),
                )
                patched += cur.rowcount
    return {"stocks": len(codes), "missing_was": len(missing), "filled": filled,
            "names": len(names), "daily_close_patched": patched, "errors": errors}


async def backfill_member_quotes(n_days: int = 20, concurrency: int = 12) -> dict:
    """已入库成分股补日K → daily_close。沪深用腾讯；北交所用新浪（腾讯 BJ 历史常只有当日）。"""
    from app.datasources import sina, tencent
    from app.datasources.codes import market_of, normalize_code

    plates = plate_flow.load_members()
    name_map = plate_flow.load_stock_names()
    codes: set[str] = set()
    for v in plates.values():
        for day_map in (v.get("stocks") or {}).values():
            for arr in (day_map or {}).values():
                codes.update(str(c) for c in (arr or []) if c)
    if not codes:
        return {"stocks": 0, "saved": {}}
    sem = asyncio.Semaphore(concurrency)
    per_date: dict[str, list[dict]] = {}
    errors = 0
    bj_n = hs_n = 0

    async def one(code: str) -> None:
        nonlocal errors, bj_n, hs_n
        async with sem:
            try:
                if market_of(code) == "bj":
                    data = await sina.kline_day(code, n_days + 5)
                    bj_n += 1
                else:
                    data = await tencent.kline_day(code, n_days + 5)
                    # BJ 误分到沪深时腾讯常只有 1 根，回退新浪
                    if len(data.get("x") or []) < 3:
                        data = await sina.kline_day(code, n_days + 5)
                        bj_n += 1
                    else:
                        hs_n += 1
            except Exception:
                errors += 1
                return
        name = name_map.get(code) or ""
        for i, d in enumerate(data.get("x") or []):
            iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            y = data["y"][i]
            prev = y[4] if len(y) > 4 else (data["y"][i - 1][1] if i else None)
            per_date.setdefault(iso, []).append({
                "stock_code": normalize_code(code),
                "stock_name": name,
                "trade_date": iso,
                "open": y[0],
                "close": y[1],
                "high": y[2],
                "low": y[3],
                "prev_close": prev,
                "volume": (data.get("vol") or [None])[i] if data.get("vol") else None,
            })

    await asyncio.gather(*(one(c) for c in sorted(codes)))
    flat = [row for rows in per_date.values() for row in rows]
    n = store.daily_close_upsert(flat, source="member_quotes_tx_sina")
    return {"stocks": len(codes), "days": len(per_date), "upserted": n,
            "errors": errors, "bj_fetches": bj_n, "hs_fetches": hs_n}
