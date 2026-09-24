"""One-shot align local ML vs origin api.zizizaizai.com (read-only)."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "docs"
DAY = "2026-09-24"
BOARD = "801159"
HDR = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://quant.zizizaizai.com/",
}


def get(url: str, timeout: int = 60):
    req = urllib.request.Request(url, headers=HDR)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def local(path: str, timeout: int = 90):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def top_rank_origin(day: str, n: int = 12):
    o = get(
        f"https://api.zizizaizai.com/v3/market/plates/17/rank/days"
        f"?date2={day}&n_days=1&data_src=1&n_type=9"
    )
    data = o.get("data") or o
    rows = data if isinstance(data, list) else (data.get("list") or data.get("rows") or [])
    out = []
    for r in rows[:n]:
        out.append(
            {
                "code": str(r.get("plate_code") or r.get("code") or ""),
                "name": r.get("plate_name") or r.get("name"),
                "score": r.get("sum_score", r.get("score")),
                "rate": r.get("sum_rate", r.get("rate")),
            }
        )
    return o, out


def top_rank_local(day: str, n: int = 12):
    l = local(f"/v3/market/plates/17/rank/columns?days=10&n_days=1&n_type=9&limit={n}")
    data = l.get("data")
    if isinstance(data, list):
        cols = data
    elif isinstance(data, dict):
        cols = data.get("columns") or data.get("list") or []
    else:
        cols = []
    col = next((c for c in cols if isinstance(c, dict) and c.get("date") == day), cols[0] if cols else {})
    if not isinstance(col, dict):
        col = {}
    rows = col.get("rows") or []
    out = []
    for r in rows[:n]:
        out.append(
            {
                "code": str(r.get("plate_code") or ""),
                "name": r.get("plate_name"),
                "score": r.get("sum_score", r.get("score")),
                "rate": r.get("sum_rate", r.get("rate")),
                "collected_at": col.get("collected_at"),
                "source": col.get("source"),
                "status": col.get("status"),
            }
        )
    return l, out, col


def cmp_lists(label: str, a: list, b: list, key="code"):
    print(f"\n=== {label} ===")
    print(f"{'rk':>2} {'o_code':8} {'o_score':>8} {'o_rate':>7} | {'l_code':8} {'l_score':>8} {'l_rate':>7} | match")
    for i in range(max(len(a), len(b))):
        oa = a[i] if i < len(a) else {}
        lb = b[i] if i < len(b) else {}
        same = oa.get(key) == lb.get(key)
        print(
            f"{i+1:2} {str(oa.get('code','')):8} {str(oa.get('score','')):>8} {str(oa.get('rate','')):>7} | "
            f"{str(lb.get('code','')):8} {str(lb.get('score','')):>8} {str(lb.get('rate','')):>7} | "
            f"{'Y' if same else 'N'}"
        )
    a_codes = [x.get(key) for x in a]
    b_codes = [x.get(key) for x in b]
    print(f"order_equal={a_codes==b_codes} set_jaccard={len(set(a_codes)&set(b_codes))/max(1,len(set(a_codes)|set(b_codes))):.2f}")


def stocks_rates(day: str, real: bool = False):
    q = f"/v3/market/plates/17/{BOARD}/stocks/rates?date1={day}&page=1&limit=10"
    if real:
        q += "&is_real=1"
    lo = local(q)
    oo = get(
        f"https://api.zizizaizai.com/v3/market/plates/17/{BOARD}/stocks/rates"
        f"?date1={day}&page=1&limit=10" + ("&is_real=1" if real else "")
    )
    def names(payload):
        data = payload.get("data") or payload
        lst = data.get("list") if isinstance(data, dict) else data
        if not isinstance(lst, list):
            return []
        out = []
        for r in lst[:10]:
            out.append(
                {
                    "code": str(r.get("stock_code") or r.get("code") or ""),
                    "name": r.get("stock_name") or r.get("name"),
                    "rate": r.get("px_change_rate", r.get("rate")),
                }
            )
        return out
    return names(oo), names(lo)


def popular(day: str):
    lo = local(f"/v3/market/plates/17/{BOARD}/stocks/rank/list?date1={day}&page=1&limit=10&with_pct=1")
    # origin uses stocks/rank
    oo = get(
        f"https://api.zizizaizai.com/v3/market/plates/17/{BOARD}/stocks/rank"
        f"?with_pct=1&limit=10&date1={day}&page=1"
    )
    def names(payload, local_shape=False):
        data = payload.get("data") or payload
        lst = data.get("list") if isinstance(data, dict) else data
        if not isinstance(lst, list):
            # maybe nested
            if isinstance(data, dict):
                for k in ("rows", "items", "stocks"):
                    if isinstance(data.get(k), list):
                        lst = data[k]
                        break
        if not isinstance(lst, list):
            return [], data
        out = []
        for r in lst[:10]:
            out.append(
                {
                    "code": str(r.get("stock_code") or r.get("code") or ""),
                    "name": r.get("stock_name") or r.get("name"),
                    "rank": r.get("rank") or r.get("hot_rank") or r.get("order"),
                    "rate": r.get("px_change_rate", r.get("rate") or r.get("pct")),
                }
            )
        return out, data
    o_list, o_raw = names(oo)
    l_list, l_raw = names(lo)
    return o_list, l_list, oo, lo


def ladder(day: str):
    lo = local(f"/v3/open/review/uplimit/hot?board={BOARD}&date1={day}")
    oo = get(f"https://api.zizizaizai.com/v3/open/review/uplimit/hot?board={BOARD}&date1={day}")
    def summary(payload):
        data = payload.get("data") or payload
        if not isinstance(data, dict):
            return {"keys": type(data).__name__, "sample": str(data)[:200]}
        stocks = data.get("stocks") or data.get("plate_stocks") or []
        # matrix form
        max_c = data.get("max_count")
        ban = data.get("ban_info")
        names = []
        if isinstance(stocks, list):
            for s in stocks[:15]:
                if isinstance(s, dict):
                    names.append(s.get("stock_name") or s.get("name") or s.get("stock_code"))
                else:
                    names.append(str(s)[:40])
        return {
            "keys": list(data.keys())[:20],
            "max_count": max_c,
            "n_stocks": len(stocks) if hasattr(stocks, "__len__") else None,
            "names_head": names[:10],
            "ban_type": type(ban).__name__,
        }
    return summary(oo), summary(lo), oo, lo


def pct(day: str, days: int = 10):
    lo = local(f"/v3/market/plates/17/{BOARD}/stocks/pct?date1={day}&days={days}")
    oo = get(f"https://api.zizizaizai.com/v3/market/plates/17/{BOARD}/stocks/pct?date1={day}&days={days}")
    def buckets(payload):
        data = payload.get("data") or payload
        # origin: {date: {intervals, stocks:{iv:[...]}}}
        if isinstance(data, dict) and day in data:
            block = data[day]
        elif isinstance(data, dict) and "stocks" in data:
            block = data
        else:
            block = data
        if not isinstance(block, dict):
            return {"raw_type": type(block).__name__}
        stocks = block.get("stocks") or {}
        counts = {k: len(v) if isinstance(v, list) else v for k, v in stocks.items()} if isinstance(stocks, dict) else {}
        heads = {}
        if isinstance(stocks, dict):
            for k, lst in stocks.items():
                if isinstance(lst, list) and lst:
                    r0 = lst[0]
                    heads[k] = r0.get("stock_name") or r0.get("name") or r0.get("stock_code")
        return {"counts": counts, "heads": heads, "intervals": block.get("intervals")}
    return buckets(oo), buckets(lo)


def main():
    print(f"DAY={DAY} BOARD={BOARD}")
    o_raw, o_top = top_rank_origin(DAY)
    l_raw, l_top, col = top_rank_local(DAY)
    print(f"local col meta: status={col.get('status')} source={col.get('source')} collected_at={col.get('collected_at')}")
    cmp_lists("rank strength top12", o_top, l_top)

    # score deltas for same code
    om = {x["code"]: x for x in o_top}
    lm = {x["code"]: x for x in l_top}
    print("\nscore deltas (intersection):")
    for c in sorted(set(om) & set(lm)):
        os, ls = om[c]["score"], lm[c]["score"]
        or_, lr = om[c]["rate"], lm[c]["rate"]
        try:
            ds = float(os) - float(ls)
            dr = float(or_) - float(lr)
        except Exception:
            ds = dr = None
        print(f"  {c} {om[c]['name']}: score {os}->{ls} d={ds}; rate {or_}->{lr} d={dr}")

    print("\n=== stocks/rates hist ===")
    o_s, l_s = stocks_rates(DAY, real=False)
    cmp_lists("rates hist", o_s, l_s)
    print("\n=== stocks/rates realtime ===")
    o_r, l_r = stocks_rates(DAY, real=True)
    cmp_lists("rates real", o_r, l_r)

    print("\n=== popular ===")
    o_p, l_p, o_pr, l_pr = popular(DAY)
    cmp_lists("popular", o_p, l_p)
    (OUT / "_align_origin_popular.json").write_text(
        json.dumps(o_pr, ensure_ascii=False)[:6000], encoding="utf-8"
    )
    (OUT / "_align_local_popular.json").write_text(
        json.dumps(l_pr, ensure_ascii=False)[:6000], encoding="utf-8"
    )

    print("\n=== ladder ===")
    o_l, l_l, o_lr, l_lr = ladder(DAY)
    print("origin", json.dumps(o_l, ensure_ascii=False))
    print("local ", json.dumps(l_l, ensure_ascii=False))

    print("\n=== pct 10d ===")
    o_pct, l_pct = pct(DAY, 10)
    print("origin", json.dumps(o_pct, ensure_ascii=False))
    print("local ", json.dumps(l_pct, ensure_ascii=False))

    (OUT / "_align_summary.json").write_text(
        json.dumps(
            {
                "day": DAY,
                "board": BOARD,
                "local_meta": {
                    "status": col.get("status"),
                    "source": col.get("source"),
                    "collected_at": col.get("collected_at"),
                },
                "origin_top": o_top,
                "local_top": l_top,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {OUT / '_align_summary.json'}")


if __name__ == "__main__":
    main()
