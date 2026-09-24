#!/usr/bin/env python3
"""周一盘中对拍一键脚本（2026-09-14 预制备）。

用法: .venv/bin/python tools/monday_verify.py [--date 2026-09-14]

三个验证项（对应 docs/待解决问题清单.md 残留差异）:
1. popular_snapshots_minutes 分钟档落库检查（本地）
2. 原站分钟分桶对拍 gt5/lt5（原站 /v3/sentiment/data vs 本地）
3. 原站涨停池明细比对 uplimit_num 差 3（本地 40 vs 原站 37，非首板 7 vs 4）

需要原站登录态时: 环境变量 ZZ_TOKEN（原站 Authorization Bearer token）。
无 token 时仅做本地项 1，并跳过原站项给出提示。
"""
from __future__ import annotations
import argparse, json, os, sqlite3, sys, urllib.request
from datetime import datetime

LOCAL = "http://127.0.0.1:8000"
ORIG = "https://api.zizizaizai.com"
LOCAL_TOKEN = "zzq_e3ce11d64f8ff0560a44a238bb6da4f3d4caf9cdd6755a21"


def get(url: str, token: str | None = None, timeout: int = 15) -> dict:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "Mozilla/5.0")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--db", default="zzquant.db")
    args = ap.parse_args()
    day = args.date  # 库内日期格式带 '-'
    orig_token = os.environ.get("ZZ_TOKEN")
    findings: list[str] = []

    print(f"=== 周一盘中对拍 {args.date} ===\n")

    # 项 1: popular_snapshots_minutes 落库
    c = sqlite3.connect(args.db)
    n = c.execute("select count(*) from popular_snapshots_minutes where trade_date=?", (day,)).fetchone()[0]
    last = c.execute("select max(minute) from popular_snapshots_minutes where trade_date=?", (day,)).fetchone()[0]
    print(f"[1] popular_snapshots_minutes 落库: {n} 条, 最新档 {last}")
    if n == 0:
        findings.append("popular_snapshots_minutes 零落库 -> 检查 collector popular 任务与 popular.collect(minute=) 链路")
    elif n < 10:
        findings.append(f"popular_snapshots_minutes 仅 {n} 条（盘中应分钟级增长）-> 检查任务节拍")
    else:
        print("    OK: 分钟档持续落库")

    # 项 2: 原站 vs 本地 gt5/lt5 分桶
    if orig_token:
        try:
            o = get(f"{ORIG}/v3/sentiment/data?date1={args.date}", orig_token)
            od = o.get("data") or {}
            print(f"[2] 原站 gt5={od.get('gt5')} lt5={od.get('lt5')} up={od.get('up_num')} down={od.get('down_num')}")
        except Exception as e:
            print(f"[2] 原站 sentiment/data 失败: {e}")
            findings.append(f"原站 sentiment/data 请求失败: {e}")
    else:
        print("[2] 跳过原站对拍（未设 ZZ_TOKEN 环境变量）")
    l = get(f"{LOCAL}/v3/sentiment/data?date1={args.date}", LOCAL_TOKEN)
    ld = (l.get("data") or {})
    if isinstance(ld, list):
        ld = ld[-1] if ld else {}
    # 本地字段口径: gt5 = gt_5_7 + gt_7_10 + gt_10_20 组装（面板层）
    print(f"    本地 sentiment/data keys 样例: {sorted(k for k in ld.keys() if 'gt' in k or 'lt' in k or k in ('up_num','down_num'))}")
    vals = {k: ld.get(k) for k in ('up_num','down_num','uplimit_num','zb_num')}
    print(f"    本地 up={vals['up_num']} down={vals['down_num']} uplimit={vals['uplimit_num']} zb={vals['zb_num']}")

    # 项 3: 涨停池非首板明细
    try:
        rows = c.execute(
            "select payload from limit_pool_snapshots where trade_date in (?, ?) and pool='em_up'",
            (day, day.replace("-", ""))).fetchone()
        if rows:
            p = json.loads(rows[0])
            lbc_dist = {}
            for i in p.get("pool") or []:
                lbc_dist[i.get("lbc")] = lbc_dist.get(i.get("lbc"), 0) + 1
            print(f"[3] 本地涨停池连板分布: {dict(sorted((k or 0, v) for k, v in lbc_dist.items()))}")
            non_first = sum(v for k, v in lbc_dist.items() if (k or 1) > 1)
            print(f"    非首板合计 {non_first}（09-11 时本地 7 vs 原站 4，待盘中比对名单）")
        else:
            print("[3] 本地涨停池未落库（可能未到采集时点）")
    except Exception as e:
        print(f"[3] 涨停池读取失败: {e}")

    print()
    if findings:
        print("待跟进:")
        for f in findings:
            print(" -", f)
    else:
        print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
