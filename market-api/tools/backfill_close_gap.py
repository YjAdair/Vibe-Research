"""补缺指定交易日 daily_close：用已有日线代码池 + 腾讯日K，不依赖东财全市场快照。"""
from __future__ import annotations
import asyncio
import sqlite3
from pathlib import Path

from app.core.store import store
from app.datasources import tencent
from app.datasources.codes import normalize_code

DB = Path(__file__).resolve().parents[1] / "zzquant.db"
NEED = {"2026-09-23"}  # 今日盘中不做正式收盘落库
N_BARS = 12
CONCURRENCY = 10


async def main() -> None:
    conn = sqlite3.connect(str(DB))
    codes = [r[0] for r in conn.execute(
        "select distinct stock_code from daily_close where trade_date='2026-09-22'"
    ).fetchall()]
    names = {
        r[0]: r[1]
        for r in conn.execute(
            "select stock_code, max(stock_name) from daily_close where trade_date='2026-09-22' group by stock_code"
        )
    }
    conn.close()
    print("universe", len(codes), "need", sorted(NEED))

    sem = asyncio.Semaphore(CONCURRENCY)
    per_date: dict[str, list[dict]] = {d: [] for d in NEED}
    errors = 0
    ok = 0

    async def one(code: str) -> None:
        nonlocal errors, ok
        async with sem:
            try:
                data = await tencent.kline_day(code, N_BARS)
            except Exception:
                errors += 1
                return
        xs, ys, vols = data.get("x") or [], data.get("y") or [], data.get("vol") or []
        by = {f"{x[:4]}-{x[4:6]}-{x[6:]}": i for i, x in enumerate(xs)}
        hit = False
        for day in NEED:
            i = by.get(day)
            if i is None:
                continue
            prev = ys[i - 1][1] if i > 0 else None
            per_date[day].append({
                "stock_code": normalize_code(code),
                "stock_name": names.get(code),
                "close": ys[i][1],
                "open": ys[i][0],
                "high": ys[i][2],
                "low": ys[i][3],
                "prev_close": prev,
                "volume": vols[i] if i < len(vols) else None,
            })
            hit = True
        if hit:
            ok += 1

    await asyncio.gather(*(one(c) for c in codes))
    print("ok_codes", ok, "errors", errors)
    for day, rows in sorted(per_date.items()):
        if len(rows) < 1000:
            print("skip thin", day, len(rows))
            continue
        meta = {
            "complete": True,
            "trade_date": day,
            "priced_count": len(rows),
            "source": "tencent_kline_gapfill",
            "note": "fill missing close after collector gap; not EM EOD snapshot",
        }
        n = store.daily_close_save(day, rows, meta)
        print("saved", day, n)

    print("dates now", store.daily_close_dates(8))


if __name__ == "__main__":
    asyncio.run(main())
