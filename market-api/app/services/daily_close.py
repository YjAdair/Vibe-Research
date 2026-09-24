"""全市场日线收盘服务：pct-tier 等预计算的落库数据基础。

对齐原站架构：定时任务（收盘后）批量落库，接口读库 O(1)，
避免每次请求对上游免费数据源做全市场扫描。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.core.cache import cache
from app.core.store import store
from app.datasources import eastmoney

logger = logging.getLogger("zzquant.daily_close")


async def backfill_day(d: str | None = None) -> int:
    """Publish one complete close snapshot. Incomplete batches never replace a known day."""
    from zoneinfo import ZoneInfo
    from app.datasources.codes import market_of
    tz = ZoneInfo("Asia/Shanghai")
    requested = d or datetime.now(tz).strftime("%Y-%m-%d")
    if len(requested) == 8:
        requested = f"{requested[:4]}-{requested[4:6]}-{requested[6:]}"
    snap = await eastmoney.market_snapshot()
    if snap.get("trade_date") != requested:
        raise ValueError("Snapshot date is not the requested close session")
    if not snap.get("complete") or int(snap.get("hs_count") or 0) <= 0 or int(snap.get("bj_count") or 0) <= 0:
        raise ValueError("Incomplete HSJ snapshot cannot be published as daily close")
    rows = []
    unpriced = []
    from app.services.ml_r1 import verified_close_time
    for s in snap["stocks"]:
        code = s["code"]
        close, prev = s.get("price"), s.get("prev_close")
        if close is None or prev is None or close <= 0 or prev <= 0:
            unpriced.append(code)
            continue
        stamp = s.get('source_timestamp')
        as_of = datetime.fromtimestamp(stamp, tz).isoformat() if isinstance(stamp, (int, float)) else None
        if not verified_close_time(as_of, requested):
            raise ValueError('Priced security has no verified closing timestamp: ' + code)
        market = {"sh": "沪", "sz": "深", "bj": "北"}[market_of(code)]
        rows.append({
            "stock_code": code,
            "stock_name": s.get("name", ""),
            "market_type": market,
            "concept": (str(s.get("concepts") or "").split(",")[0] or str(s.get("industry") or "")).strip(),
            "close": float(close),
            "prev_close": float(prev),
            "high": float(s["high"]) if s.get("high") else None,
            "low": float(s["low"]) if s.get("low") else None,
            "open": float(s["open"]) if s.get("open") else None,
            "circulation_value": float(s["circulation_value"]) if s.get("circulation_value") else None,
            "turnover_ratio": float(s["turnover_ratio"]) if s.get("turnover_ratio") is not None else None,
            "vol_ratio": float(s["vol_ratio"]) if s.get("vol_ratio") is not None else None,
        })
    if not rows:
        raise ValueError("Close snapshot contained no priced A-share rows")
    metadata = {
        "complete": True,
        "trade_date": requested,
        "source": snap["source"],
        "source_as_of": snap["source_as_of"],
        "source_as_of_max": snap["source_as_of_max"],
        "collected_at": snap["collected_at"],
        "hs_count": snap["hs_count"],
        "bj_count": snap["bj_count"],
        "priced_count": len(rows),
        "unpriced_count": len(unpriced),
        "skipped_non_a": len(snap.get("skipped_non_a") or []),
        "coverage": snap["coverage"],
        "amount_unit": snap["amount_unit"],
        "universe_note": "published close rows are timestamp-verified priced A-shares; unpriced names remain unknown",
        "unpriced_codes": unpriced, "all_priced_rows_eod_verified": True,
    }
    n = store.daily_close_save(requested, rows, metadata)
    logger.info("daily_close saved %s: %d rows hs=%s bj=%s", requested, n, snap["hs_count"], snap["bj_count"])
    return n


async def backfill_history(n_days: int = 40, concurrency: int = 15) -> dict:
    """回填腾讯日K。已发布收盘日只补缺失 OHLC，不覆盖 close/prev_close。

    全市场 ~5558 只，并发 15 约 1.5-2 分钟（一次性成本）。
    之后每日收盘后由调度器 increment 落库当日快照（~0.5s）。
    """
    from app.datasources import tencent
    from app.datasources.codes import normalize_code, to_tencent_symbol

    snap = await eastmoney.market_snapshot()
    all_codes = [str(s.get("code")) for s in snap.get("stocks") or [] if s.get("code")]
    meta_map = {
        str(s.get("code")): {
            "name": s.get("name") or "",
            "industry": s.get("industry") or "",
            "concepts": s.get("concepts") or "",
            "prev_close": s.get("prev_close"),
        }
        for s in snap.get("stocks") or []
    }

    have_dates = set(store.daily_close_dates(limit=n_days + 5))
    sem = asyncio.Semaphore(concurrency)
    per_date: dict[str, list[dict]] = {}
    errors = 0

    async def fetch_one(code: str) -> None:
        nonlocal errors
        async with sem:
            try:
                data = await tencent.kline_day(code, n_days + 8)
            except Exception:
                errors += 1
                return
        meta = meta_map.get(code) or {}
        for i, d in enumerate(data['x']):
            iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            row = {
                'stock_code': normalize_code(code),
                'stock_name': meta.get('name'),
                'market_type': meta.get('industry'),
                'concept': meta.get('concepts'),
                'trade_date': iso,
                'open': data['y'][i][0],
                'close': data['y'][i][1],
                'high': data['y'][i][2],
                'low': data['y'][i][3],
                # 北交所代码腾讯日K只返回当日 1 根，i=0 时回退东财快照的 prev_close
                'prev_close': data['y'][i-1][1] if i else meta.get('prev_close'),
                'volume': data['vol'][i],
            }
            per_date.setdefault(iso, []).append(row)

    await asyncio.gather(*(fetch_one(c) for c in all_codes), return_exceptions=True)
    saved = {}
    for d, rows in per_date.items():
        # 回填历史日：全部个股请求无错时视为完整批次（历史日退市股不在今日快照，
        # 今日新股在历史日无行情，行数天然小于今日全市场数）
        try:
            saved[d] = store.daily_close_save(d, rows, metadata={'complete': errors == 0})
        except ValueError:
            saved[d] = 0
    return {'days': len(per_date), 'saved': saved, 'errors': errors, 'stocks': len(all_codes)}


async def patch_turnover(dates: list[str] | None = None, codes: list[str] | None = None, concurrency: int = 3) -> dict:
    """patch_turnover: fill missing turnover_ratio / circulation_value / vol_ratio.

    Source priority: tencent realtime batch (one request for all codes) ->
    eastmoney kline per-stock (vol_ratio needs 5-day average volume).
    eastmoney push2his currently disconnects; it must not block publishing.
    """
    from app.datasources import eastmoney, tencent

    target = dates or [d for d in store.daily_close_dates(10)]
    if not target:
        return {'dates': [], 'patched': 0, 'errors': 0}
    if not codes:
        latest = max(target)
        codes = [r['stock_code'] for r in store.daily_close_range(latest, 1) if r['trade_date'] == latest]
    collect_rows: dict[str, list[dict]] = {d: [] for d in target}
    sem = asyncio.Semaphore(concurrency)
    errors = 0

    # 1) 腾讯实时行情批量: 一次请求覆盖全部 codes(换手率/流通市值)
    latest = max(target)
    quotes: dict[str, dict] = {}
    try:
        quotes = await tencent.realtime(codes)
    except Exception:
        quotes = {}
    tencent_rows: list[dict] = []
    for c in codes:
        q = quotes.get(c) or {}
        row = {"stock_code": c}
        if q.get("turnover"):
            row["turnover_ratio"] = q["turnover"]
        if q.get("circulation_value"):
            row["circulation_value"] = q["circulation_value"]
        if len(row) > 1:
            tencent_rows.append(row)
    tencent_patched = store.daily_close_patch_turnover(latest, tencent_rows) if tencent_rows else 0

    # 2) 东财日K逐股补 vol_ratio: 腾讯不给历史均量。仅对还缺 vol_ratio 的股票,
    #    且整体限时, 失败静默跳过(vol_ratio 是增强字段, 不阻塞发布)
    async def one(code: str) -> None:
        nonlocal errors
        async with sem:
            try:
                bars = await eastmoney.kline_history(code, max(len(target) + 10, 20))
            except Exception:
                errors += 1
                return
        # 按 date 建索引，量比 = 当日成交量 / 前5日平均成交量（全日口径）
        bar_by_date = {b['date']: b for b in bars}
        dates_sorted = sorted(bar_by_date.keys())
        for bar in bars:
            d = bar.get('date')
            if d not in collect_rows:
                continue
            close, vol, tr = bar.get("close"), bar.get("volume"), bar.get("turnover_rate")
            row = {"stock_code": code}
            if tr:
                row["turnover_ratio"] = tr
            if close and vol and tr:
                shares = vol * 100 / (tr / 100.0)
                row["circulation_value"] = shares * close
            idx = dates_sorted.index(d)
            if idx >= 5 and vol:
                prev5 = dates_sorted[idx-5:idx]
                avg5 = sum(bar_by_date[p].get('volume') or 0 for p in prev5) / 5
                if avg5 > 0:
                    row["vol_ratio"] = round(vol / avg5, 3)
            if len(row) > 1:
                collect_rows[d].append(row)

    # 整体限时: 东财逐股补 vol_ratio 最多 60s, 超时放弃剩余(增强字段, 不阻塞发布)
    try:
        async with asyncio.timeout(60):
            await asyncio.gather(*(one(c) for c in codes), return_exceptions=True)
    except TimeoutError:
        pass
    patched = {d: store.daily_close_patch_turnover(d, rows) for d, rows in collect_rows.items()}
    total = sum(patched.values())
    return {"dates": target, "patched": patched, "total": total + tencent_patched, "errors": errors,
            "stocks": len(codes), "tencent_patched": tencent_patched,
            "source": "tencent_realtime+eastmoney_kline_volratio"}


async def patch_ohlc(dates: list[str] | None = None, concurrency: int = 16, sample_codes: list[str] | None = None) -> dict:
    """用腾讯日K给已发布 daily_close 补缺失 open/high/low，不覆盖 close/prev_close。"""
    from app.datasources import tencent
    from app.datasources.codes import normalize_code

    target = dates or [d for d in store.daily_close_dates(12)
                       if sum(1 for r in store.daily_close_range(d, 1) if r['trade_date'] == d and r.get('open') is not None)
                       < len([r for r in store.daily_close_range(d, 1) if r['trade_date'] == d]) * 0.5]
    if not target:
        return {"dates": [], "patched": {}, "stocks": 0, "errors": 0}
    sem = asyncio.Semaphore(concurrency)
    per_date: dict[str, list[dict]] = {d: [] for d in target}
    errors = 0

    def pick_codes(d: str) -> list[str]:
        rows = [r for r in store.daily_close_range(d, 1) if r['trade_date'] == d]
        codes = [r['stock_code'] for r in rows if r.get('open') is None]
        if sample_codes:
            wanted = {normalize_code(c) for c in sample_codes}
            codes = [c for c in codes if c in wanted]
        return codes

    codes_by_date = {d: pick_codes(d) for d in target}

    async def one(code: str) -> None:
        nonlocal errors
        async with sem:
            try:
                data = await tencent.kline_day_by_symbol(
                    __import__('app.datasources.codes', fromlist=['to_tencent_symbol']).to_tencent_symbol(code), 40)
            except Exception:
                errors += 1
                return
        bar_by_date = {d.replace('-', ''): bar for d, bar in zip(data['x'], data['y'])}
        for d in target:
            bar = bar_by_date.get(d.replace('-', ''))
            if not bar or code not in codes_by_date.get(d, []):
                continue
            per_date[d].append({
                'stock_code': code,
                'open': bar[0], 'high': bar[2], 'low': bar[3],
            })

    all_codes = sorted({c for cs in codes_by_date.values() for c in cs})
    await asyncio.gather(*(one(c) for c in all_codes), return_exceptions=True)
    patched = {d: store.daily_close_patch_ohlc(d, rows) for d, rows in per_date.items()}
    return {"dates": target, "patched": patched, "stocks": len(all_codes), "errors": errors}

def _range_pct(rows_by_date: dict[str, dict[str, dict]], dates: list[str], code: str) -> float | None:
    """区间累计涨幅：(末日收盘 - 基日收盘) / 基日收盘。基日 = 区间首日前一交易日。"""
    end = rows_by_date.get(dates[-1], {}).get(code)
    base = rows_by_date.get(dates[0], {}).get(code)
    if not end or not base:
        return None
    c_end = end.get("close")
    c_base = base.get("prev_close") or base.get("close")
    if not c_end or not c_base:
        return None
    return round((c_end - c_base) / c_base * 100, 2)


if __name__ == "__main__":
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Patch published daily_close rows with Tencent OHLC")
    parser.add_argument("--dates", nargs="*", default=[], help="YYYY-MM-DD, default recent published dates missing OHLC")
    parser.add_argument("--days", type=int, default=12)
    parser.add_argument("--concurrency", type=int, default=16)
    args = parser.parse_args()

    async def _run():
        dates = args.dates
        if not dates:
            from app.core.store import store as db
            dates = []
            for day in db.daily_close_dates(args.days):
                rows = [r for r in db.daily_close_range(day, 1) if r["trade_date"] == day]
                if rows and sum(1 for r in rows if r.get("open") is not None) < len(rows) * 0.5:
                    dates.append(day)
        print(await patch_ohlc(dates, concurrency=args.concurrency))

    asyncio.run(_run())
