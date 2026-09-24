"""策略表现：打板次日回测。次日开盘价未纳入已发布快照前不计算收益。"""
from __future__ import annotations
from app.core.store import store
from app.services import pools

INITIAL_CASH = 1_000_000.0
CAP_PER_DAY = 8
BUDGET_PER_NAME = 50_000.0


def _iso(day: str) -> str:
    d = str(day).replace("-", "")
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def _close_map(date: str) -> dict[str, dict]:
    rows = [r for r in store.daily_close_range(date, 1) if r["trade_date"] == date]
    return {r["stock_code"]: r for r in rows}


def _pool(date: str) -> list[dict]:
    snap = pools.published("up", date)
    return list((snap or {}).get("pool") or [])


def _calendar() -> list[str]:
    pool_dates = []
    for kind in ("em_up",):
        pass
    dates = store.daily_close_dates(40)
    return sorted(dates)


def backtest(window: int = 12, cap_per_day: int = CAP_PER_DAY) -> dict:
    """涨停次日开盘买入、当日收盘卖出。没有次日开盘价的交易日不入账。"""
    calendar = _calendar()
    if len(calendar) < 2:
        return {
            "status": "missing",
            "total_return_pct": None,
            "win_rate_pct": None,
            "avg_return_pct": None,
            "total_trades": 0,
            "equity": [],
            "note": "need published close calendar",
        }
    days = calendar[-(window + 1):]
    cash = INITIAL_CASH
    equity_rows = []
    trades = []
    daily = {}
    skipped_no_open = 0
    eligible = 0
    for i, buy_day in enumerate(days[1:], start=1):
        signal_day = days[i - 1]
        pool = _pool(signal_day)
        eligible += len(pool)
        buy_map = _close_map(buy_day)
        names = []
        for row in pool:
            code = str(row.get("c") or row.get("code") or "")
            quote = buy_map.get(code)
            if not quote or not quote.get("open") or not quote.get("close") or not quote.get("prev_close"):
                skipped_no_open += 1
                continue
            names.append((code, row, quote))
            if len(names) >= cap_per_day:
                break
        records = []
        positions = []
        day_pnl = 0.0
        for code, row, quote in names:
            buy = float(quote["open"])
            sell = float(quote["close"])
            qty = int(BUDGET_PER_NAME // buy / 100) * 100
            if qty <= 0:
                continue
            cost = qty * buy
            proceeds = qty * sell
            pnl = round(proceeds - cost, 2)
            ret = round((sell / buy - 1) * 100, 2)
            cash += pnl
            day_pnl += pnl
            rec = {
                "trade_time": f"{buy_day} 09:30:00",
                "symbol_code": code,
                "symbol_name": row.get("n") or row.get("name") or quote.get("stock_name") or code,
                "trade_type": "买入",
                "trade_price": buy,
                "trade_balance": cost,
                "today_income_balance": pnl,
                "today_pnl": pnl,
                "return_pct": ret,
                "qty": qty,
                "signal_date": signal_day,
                "sell_price": sell,
                "sell_time": f"{buy_day} 15:00:00",
            }
            records.append(rec)
            trades.append(rec)
            positions.append({
                "symbol_code": code,
                "symbol_name": rec["symbol_name"],
                "status": "已平仓",
                "qty": qty,
                "cost_price": buy,
                "last_price": sell,
                "market_value": proceeds,
                "pnl": pnl,
            })
            records.append({
                **rec,
                "trade_time": f"{buy_day} 15:00:00",
                "trade_type": "卖出",
                "trade_price": sell,
                "trade_balance": proceeds,
            })
        equity = round(cash, 2)
        equity_rows.append({
            "date": buy_day,
            "equity": equity,
            "total_balance": equity,
            "enable_balance": equity,
            "cash_balance": equity,
            "market_value": 0.0,
            "income_balance": round(equity - INITIAL_CASH, 2),
            "today_pnl_sum": round(day_pnl, 2),
        })
        daily[buy_day] = {
            "date": buy_day,
            "records": records,
            "positions": positions,
            "today_pnl_sum": round(day_pnl, 2),
        }
    pnls = [t["today_pnl"] for t in trades]
    wins = sum(1 for x in pnls if x > 0)
    total_ret = round((cash / INITIAL_CASH - 1) * 100, 2) if trades else None
    status = "ok" if trades else ("missing_open_snapshot" if skipped_no_open or eligible else "missing")
    return {
        "status": status,
        "total_return_pct": total_ret,
        "win_rate_pct": round(wins / len(pnls) * 100, 1) if pnls else None,
        "avg_return_pct": round(sum(t["return_pct"] for t in trades) / len(trades), 2) if trades else None,
        "total_trades": len(trades),
        "eligible_limit_up_names": eligible,
        "skipped_no_open": skipped_no_open,
        "initial_cash": INITIAL_CASH,
        "latest_equity": equity_rows[-1]["equity"] if equity_rows else INITIAL_CASH,
        "equity": equity_rows,
        "daily": daily,
        "source": "published_limit_pools+daily_close_open",
        "note": "buy next-day open of limit-up names, sell same-day close; days without open are skipped, not filled with close",
    }


async def strategy_performance(window: int = 12, cap_per_day: int = CAP_PER_DAY) -> dict:
    data = backtest(window, cap_per_day)
    return {k: data[k] for k in (
        "status", "total_return_pct", "win_rate_pct", "avg_return_pct", "total_trades",
        "eligible_limit_up_names", "skipped_no_open", "latest_equity", "initial_cash", "source", "note",
    ) if k in data}


async def strategy_equity(window: int = 12) -> list[dict]:
    return backtest(window).get("equity") or []


async def strategy_daily_details(date: str | None = None) -> dict:
    data = backtest()
    daily = data.get("daily") or {}
    def _with_manual(hit: dict) -> dict:
        day = hit.get("date")
        if not day:
            return hit
        manual = manual_buys_of(day)
        if not manual:
            return hit
        records = list(hit.get("records") or [])
        codes = {str(r.get("symbol_code") or "") for r in records}
        for b in manual:
            if b.get("code") in codes:
                continue
            records.append({
                "trade_time": f"{day} {b.get('time') or '00:00:00'}",
                "symbol_code": b.get("code"),
                "symbol_name": b.get("name"),
                "trade_type": "买入",
                "trade_price": b.get("price"),
                "trade_balance": None,
                "today_income_balance": 0,
                "today_pnl": 0,
                "source": "manual_trigger",
            })
        return {**hit, "records": records, "manual_buys": manual}
    if date:
        day = _iso(date)
        hit = daily.get(day)
        if not hit:
            return {"date": day, "status": "missing", "records": [], "positions": [], "today_pnl_sum": 0}
        if not hit.get("records"):
            return _with_manual({**hit, "status": "skipped_no_open"})
        return _with_manual({**hit, "status": "ok"})
    last = data.get("equity")[-1]["date"] if data.get("equity") else None
    hit = daily.get(last) if last else None
    return {**(hit or {"records": [], "positions": [], "today_pnl_sum": 0, "date": last}), "status": "ok" if hit else "missing"}


def manual_buys_add(code: str, name: str = "", price: float | None = None) -> dict:
    """全景沙盘手动点火买入：记录到 kv，当日流水合并展示（模拟）。"""
    from app.core.store import store
    from datetime import datetime as _dt
    day = _dt.now().strftime("%Y-%m-%d")
    buys = store.kv_get("panorama_manual_buys", {}) or {}
    day_list = buys.get(day) or []
    entry = {
        "code": str(code),
        "name": name or str(code),
        "price": price,
        "time": _dt.now().strftime("%H:%M:%S"),
        "source": "manual_trigger",
    }
    day_list = [b for b in day_list if b.get("code") != entry["code"]]
    day_list.append(entry)
    buys[day] = day_list
    store.kv_set("panorama_manual_buys", buys)
    return {"code": entry["code"], "name": entry["name"], "date": day, "count": len(day_list)}


def manual_buys_of(date: str) -> list[dict]:
    from app.core.store import store
    buys = store.kv_get("panorama_manual_buys", {}) or {}
    return buys.get(str(date)) or []
