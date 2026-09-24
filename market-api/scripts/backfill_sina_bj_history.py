#!/usr/bin/env python3
"""为北交所 920 段（2026-09-10 统一改码）回填改码前历史日线。

背景：09-10 北交所证券代码统一切换到 920 段，daily_close 只有改码后数据，
20 日累计涨幅窗口全部缺基期。新浪 bj92xxxx 日线接口直接返回改码前完整历史
（已验证 920821：days=5/10/20 cum 39.34/42.8/37.07 与原站逐值一致），
故用其补齐历史，只插入本地缺失的 (trade_date, stock_code) 行，不覆盖已有数据。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND / "zzquant.db"
SINA_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2026-05-01", help="回填起始日（默认 05-01，覆盖 20 日窗口）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 只（0=全部）")
    parser.add_argument("--sleep", type=float, default=0.35, help="请求间隔秒")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    codes = [
        r["stock_code"]
        for r in conn.execute(
            "SELECT DISTINCT stock_code FROM daily_close WHERE stock_code LIKE '92%' ORDER BY stock_code"
        ).fetchall()
    ]
    have = {
        (r["trade_date"], r["stock_code"])
        for r in conn.execute(
            "SELECT trade_date, stock_code FROM daily_close WHERE stock_code LIKE '92%'"
        ).fetchall()
    }
    names = {
        r["stock_code"]: r["stock_name"]
        for r in conn.execute(
            "SELECT stock_code, MAX(stock_name) AS stock_name FROM daily_close WHERE stock_code LIKE '92%' GROUP BY stock_code"
        ).fetchall()
    }
    if args.limit:
        codes = codes[: args.limit]
    print(f"920 codes: {len(codes)}, existing rows: {len(have)}")

    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    inserted = failed = 0
    errors: list[tuple[str, str]] = []
    with httpx.Client(trust_env=False, headers=headers, timeout=15) as client:
        for idx, code in enumerate(codes, 1):
            try:
                r = client.get(SINA_URL, params={"symbol": f"bj{code}", "scale": 240, "ma": "no", "datalen": 300})
                r.raise_for_status()
                bars = r.json()
                if not isinstance(bars, list) or not bars:
                    raise ValueError(f"empty history: {r.text[:80]}")
                n_new = 0
                for i, b in enumerate(bars):
                    day = b["day"]
                    if day < args.since:
                        continue
                    if (day, code) in have:
                        continue
                    prev = float(bars[i - 1]["close"]) if i > 0 else None
                    conn.execute(
                        "INSERT OR IGNORE INTO daily_close (trade_date, stock_code, stock_name, market_type, close, prev_close, high, low, open) VALUES(?,?,?,?,?,?,?,?,?)",
                        (day, code, names.get(code, ""), "北", float(b["close"]), None, float(b["high"]), float(b["low"]), float(b["open"])),
                    )
                    n_new += 1
                conn.commit()
                inserted += n_new
                print(f"[{idx}/{len(codes)}] {code} +{n_new} (bars={len(bars)})", flush=True)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append((code, str(exc)[:120]))
                print(f"[{idx}/{len(codes)}] {code} FAILED: {exc}", flush=True)
            time.sleep(args.sleep)
    print(f"\ndone: inserted={inserted} failed={failed}")
    for code, err in errors:
        print(f"  {code}: {err}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
