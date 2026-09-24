"""Quick hist day align for rates/popular/ladder/pct on 2026-09-23."""
import json
import urllib.request

DAY = "2026-09-23"
BOARD = "801159"
HDR = {"User-Agent": "Mozilla/5.0", "Referer": "https://quant.zizizaizai.com/"}


def get(url: str):
    req = urllib.request.Request(url, headers=HDR)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def local(path: str):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=90) as r:
        return json.loads(r.read().decode())


def codes(payload, n=10):
    data = payload.get("data") or payload
    lst = data.get("list") if isinstance(data, dict) else data
    if not isinstance(lst, list):
        return []
    out = []
    for r in lst[:n]:
        out.append(str(r.get("stock_code") or r.get("code") or ""))
    return out


print("=== rates", DAY, "===")
o = get(f"https://api.zizizaizai.com/v3/market/plates/17/{BOARD}/stocks/rates?date1={DAY}&page=1&limit=10")
l = local(f"/v3/market/plates/17/{BOARD}/stocks/rates?date1={DAY}&page=1&limit=10")
oc, lc = codes(o), codes(l)
print("origin", oc)
print("local ", lc)
print("equal", oc == lc)

print("\n=== popular", DAY, "===")
o = get(f"https://api.zizizaizai.com/v3/market/plates/17/{BOARD}/stocks/rank?with_pct=1&limit=10&date1={DAY}&page=1")
l = local(f"/v3/market/plates/17/{BOARD}/stocks/rank/list?date1={DAY}&page=1&limit=10&with_pct=1")
oc, lc = codes(o), codes(l)
print("origin", oc)
print("local ", lc)
print("equal", oc == lc, "jaccard", len(set(oc)&set(lc))/max(1,len(set(oc)|set(lc))))

print("\n=== pct counts", DAY, "===")
o = get(f"https://api.zizizaizai.com/v3/market/plates/17/{BOARD}/stocks/pct?date1={DAY}&days=10")
l = local(f"/v3/market/plates/17/{BOARD}/stocks/pct?date1={DAY}&days=10")
od, ld = o.get("data") or o, l.get("data") or l
ob = od.get(DAY) if isinstance(od, dict) else od
lb = ld.get(DAY) if isinstance(ld, dict) else ld
# maybe date key without dashes
if not isinstance(ob, dict):
    print("origin data keys", list(od)[:5] if isinstance(od, dict) else type(od))
else:
    os_ = ob.get("stocks") or {}
    print("origin", {k: len(v) for k, v in os_.items() if isinstance(v, list)})
    if os_.get("80-100"):
        print(" o 80+", [(x.get("stock_name"), x.get("cum_pct")) for x in os_["80-100"][:2]])
if isinstance(lb, dict):
    ls_ = lb.get("stocks") or {}
    print("local ", {k: len(v) for k, v in ls_.items() if isinstance(v, list)})
    if ls_.get("80-100"):
        print(" l 80+", [(x.get("stock_name"), x.get("cum_pct")) for x in ls_["80-100"][:2]])
else:
    print("local data keys", list(ld)[:5] if isinstance(ld, dict) else type(ld), json.dumps(l, ensure_ascii=False)[:400])
