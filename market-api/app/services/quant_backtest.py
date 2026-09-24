"""量化编辑器：沙箱回测引擎 + 策略存储 + 模拟盘，协议对齐原站 service.quant_backtest_service。

引擎行为（与原站实测错误信息对齐）：
1. 代码校验：必须有 initialize 和 handle_data 两个函数，否则报 Missing。
2. 执行 initialize(context)，context.security 或 context.universe 必须给出标的。
3. 从 daily_close 已发布快照加载数据；一个标的都加载不到则报 No data loaded。
4. 按交易日循环执行 handle_data(context, data)，order() 下单按当日收盘价成交（A 股 T+1）；
   停牌日（当日无行情）持仓沿用最后已知收盘价估值，卖出成本按持仓比例结转。
安全：exec 沙箱只暴露引擎 API，禁止 import/网络/文件/危险内建。
"""
from __future__ import annotations

import math
import re
import sqlite3
import threading
import traceback
from datetime import datetime

from app.core.store import store


_BANNED = re.compile(r'(^|[^\w.])import\s|^\s*from\s+\S+\s+import|__import__|open\s*\(|exec\s*\(|eval\s*\(|compile\s*\(|globals\s*\(|locals\s*\(|os\.|sys\.|subprocess|getattr\s*\(|setattr\s*\(|delattr\s*\(|input\s*\(|breakpoint\s*\(')

_lock = threading.Lock()
_tables_ready = False


def _ensure_tables() -> None:
    global _tables_ready
    if _tables_ready:
        return
    with _lock:
        if _tables_ready:
            return
        with store._conn() as conn:  # noqa: SLF001 - 与社区模块相同的建表模式
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS quant_strategies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT DEFAULT 'demo',
                    name TEXT NOT NULL,
                    code TEXT NOT NULL,
                    status INTEGER DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS quant_paper_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id INTEGER NOT NULL,
                    user_id TEXT DEFAULT 'demo',
                    status TEXT DEFAULT 'running',
                    created_at TEXT
                );
                """
            )
        _tables_ready = True


_ensure_tables()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def validate_code(code: str) -> None:
    """先做静态校验，再进沙箱。双保险。"""
    if not code or not code.strip():
        raise ValueError("Empty strategy code")
    if _BANNED.search(code):
        raise ValueError("Code rejected: import/open/exec/eval and filesystem access are not allowed")
    if "def initialize" not in code or "def handle_data" not in code:
        raise ValueError("Missing initialize or handle_data")


class _Log:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, msg: object) -> None:
        self.messages.append(f"{msg}")

    def warning(self, msg: object) -> None:
        self.messages.append(f"[warn] {msg}")

    def error(self, msg: object) -> None:
        self.messages.append(f"[error] {msg}")


class _Portfolio:
    def __init__(self, cash: float) -> None:
        self._cash = float(cash)
        self.positions: dict[str, dict] = {}

    @property
    def cash(self) -> float:
        return round(self._cash, 2)


class _Context:
    """原站 Context 字段：current_dt, end_date, frequency, portfolio, security, universe, start_date。"""

    def __init__(self, start_date: str, end_date: str, frequency: str, cash: float) -> None:
        self.current_dt = start_date
        self.start_date = start_date
        self.end_date = end_date
        self.frequency = frequency
        self.portfolio = _Portfolio(cash)
        self.security: object = None
        self.universe: list = []


class _Data:
    """data.current(security, field) 与 data[security][field] 两种访问方式。"""

    def __init__(self, quotes: dict[str, dict]) -> None:
        self._quotes = quotes

    def current(self, security: str, field: str = "close") -> float | None:
        row = self._quotes.get(str(security))
        if row is None:
            return None
        mapping = {
            "close": "close", "close_px": "close", "open": "open", "open_px": "open",
            "high": "high", "high_px": "high", "low": "low", "low_px": "low",
            "prev_close": "prev_close", "last_px": "close", "price": "close",
        }
        col = mapping.get(field, field)
        val = row.get(col)
        return None if val is None else float(val)

    def __getitem__(self, security: str) -> dict:
        row = self._quotes.get(str(security)) or {}

        class _Row:
            def __init__(self, r):
                self._r = r

            def __getattr__(self, name):
                return self._r.get(name)

        return _Row(row)


def _load_history(codes: list[str], start: str, end: str) -> dict[str, list[dict]]:
    """读已发布 daily_close 快照，按代码组织成时间序列。"""
    if not codes:
        return {}
    with store._conn() as conn:  # noqa: SLF001
        ph = ",".join("?" * len(codes))
        rows = conn.execute(
            f"SELECT trade_date, stock_code, stock_name, open, high, low, close, prev_close "
            f"FROM daily_close WHERE stock_code IN ({ph}) AND trade_date BETWEEN ? AND ? "
            f"ORDER BY trade_date",
            [*codes, start, end],
        ).fetchall()
    history: dict[str, list[dict]] = {c: [] for c in codes}
    for r in rows:
        history.setdefault(str(r["stock_code"]), []).append(dict(r))
    return history


def _codes_from_context(ctx: _Context) -> list[str]:
    codes: list[str] = []
    for v in (getattr(ctx, "security", None), getattr(ctx, "universe", None)):
        if v is None:
            continue
        vals = v if isinstance(v, (list, tuple, set)) else [v]
        for x in vals:
            c = str(x).strip()
            if c and c not in codes:
                codes.append(c)
    return codes


def run_backtest(code: str, start_date: str, end_date: str, initial_capital: float = 1_000_000.0,
                 frequency: str = "day") -> dict:
    validate_code(code)
    ctx = _Context(start_date, end_date, frequency, initial_capital)
    logs: list[str] = []

    # 1) 执行 initialize（在受控命名空间里）
    ns: dict = {"__builtins__": _safe_builtins()}
    order_logs: list[str] = []

    def make_order(ctx_ref: _Context, day: str, quotes: dict[str, dict]):
        def order(security: object, amount: object = 0) -> bool:
            sec = str(security)
            row = quotes.get(sec)
            if row is None or row.get("close") is None:
                order_logs.append(f"order ignored: no data for {sec} @ {day}")
                return False
            price = float(row["close"])
            try:
                qty = int(amount)
            except (TypeError, ValueError):
                return False
            if qty == 0:
                return False
            cost = qty * price
            pf = ctx_ref.portfolio
            if qty > 0 and cost > pf._cash:
                order_logs.append(f"order rejected: insufficient cash @ {day} {sec}")
                return False
            if qty > 0:
                pf._cash -= cost
                pos = pf.positions.setdefault(sec, {"qty": 0, "cost": 0.0, "name": row.get("stock_name"), "buy_date": None})
                pos["qty"] += qty
                pos["cost"] += cost
                if pos.get("buy_date") is None:
                    pos["buy_date"] = day  # A 股 T+1：当日买入当日不可卖
            else:
                pos = pf.positions.get(sec)
                if not pos or pos["qty"] <= 0:
                    return False
                if pos.get("buy_date") == day:
                    order_logs.append(f"order rejected: T+1 rule, same-day buy not sellable @ {day} {sec}")
                    return False
                sell_qty = min(-qty, pos["qty"])
                proceeds = sell_qty * price
                pf._cash += proceeds
                # 卖出成本按剩余数量比例结转；按卖出价扣减会让分批交易后的持仓成本失真
                pos["cost"] = round(pos["cost"] * (pos["qty"] - sell_qty) / pos["qty"], 2)
                pos["qty"] -= sell_qty
                if pos["qty"] == 0:
                    pos["buy_date"] = None  # 清仓后重新买入重新计 T+1
            return True
        return order

    log_obj = _Log()
    try:
        exec(code, ns)  # noqa: S102 - 沙箱：受限 builtins + 静态黑名单
        ns["initialize"](ctx)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Initialize error: {exc}") from exc

    codes = _codes_from_context(ctx)
    if not codes:
        raise ValueError("No securities defined in initialize (set context.security or context.universe)")

    history = _load_history(codes, start_date, end_date)
    loaded = [c for c, rows in history.items() if rows]
    if not loaded:
        raise ValueError("No data loaded for any security")

    # 3) 按交易日循环 handle_data
    all_days = sorted({r["trade_date"] for rows in history.values() for r in rows})
    handle = ns["handle_data"]
    equity_curve: list[dict] = []
    last_close: dict[str, float] = {}  # 停牌日估值用的最后已知收盘价
    for day in all_days:
        ctx.current_dt = day
        quotes = {c: next((r for r in rows if r["trade_date"] == day), None) for c, rows in history.items()}
        quotes = {c: r for c, r in quotes.items() if r}
        data = _Data(quotes)
        # handle_data 的 __globals__ 就是 ns 本身，必须直接注入，复制 dict 无效
        ns["order"] = make_order(ctx, day, quotes)
        ns["log"] = log_obj
        ns["data"] = data
        try:
            handle(ctx, data)
        except Exception as exc:
            log_obj.error(f"handle_data error @ {day}: {exc}")
        # 收盘估值：现金 + 持仓市值；停牌日（当日无行情行）沿用最后已知收盘价，避免市值归零造成假回撤
        mv = 0.0
        for sec, pos in ctx.portfolio.positions.items():
            row = quotes.get(sec)
            if row and row.get("close") is not None:
                last_close[sec] = float(row["close"])
            price = last_close.get(sec)
            if price is not None and pos["qty"] != 0:
                mv += pos["qty"] * price
        equity_curve.append({"date": day, "equity": round(ctx.portfolio._cash + mv, 2)})

    logs = log_obj.messages + order_logs

    # 4) 指标计算
    metrics = _metrics(equity_curve, initial_capital)
    return {"metrics": metrics, "equity_curve": equity_curve, "logs": logs[:200]}


def _metrics(equity_curve: list[dict], initial_capital: float) -> dict:
    if not equity_curve:
        return {"total_return": 0.0, "annual_return": 0.0, "max_drawdown": 0.0, "sharpe_ratio": 0.0, "volatility": 0.0}
    values = [float(e["equity"]) for e in equity_curve]
    total_return = (values[-1] / initial_capital - 1) * 100 if initial_capital else 0.0
    n = len(values)
    days = max(n - 1, 1)
    annual = ((values[-1] / initial_capital) ** (252 / days) - 1) * 100 if initial_capital and values[-1] > 0 else -100.0
    peak = values[0]
    mdd = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, (v / peak - 1) * 100)
    rets = [(values[i] / values[i - 1] - 1) for i in range(1, n) if values[i - 1] > 0]
    vol = (math.sqrt(sum(r * r for r in rets) / len(rets)) * math.sqrt(252) * 100) if rets else 0.0
    sharpe = (sum(rets) / len(rets) / (math.sqrt(sum((r - sum(rets) / len(rets)) ** 2 for r in rets) / len(rets)) * math.sqrt(252))) if len(rets) > 1 and any(r != rets[0] for r in rets) else 0.0
    rnd = lambda x: round(x, 2)  # noqa: E731
    return {"total_return": rnd(total_return), "annual_return": rnd(annual), "max_drawdown": rnd(mdd), "sharpe_ratio": rnd(sharpe), "volatility": rnd(vol)}


def _safe_builtins() -> dict:
    """最小 builtins 白名单：数值/基础容器可用，IO 和反射一律不给。"""
    allowed = {
        "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict, "divmod": divmod,
        "enumerate": enumerate, "filter": filter, "float": float, "int": int, "isinstance": isinstance,
        "len": len, "list": list, "map": map, "max": max, "min": min, "pow": pow, "print": lambda *a: None,
        "range": range, "reversed": reversed, "round": round, "set": set, "sorted": sorted,
        "str": str, "sum": sum, "tuple": tuple, "zip": zip, "True": True, "False": False, "None": None,
    }
    return allowed


# ---- 策略 CRUD ----

def strategy_save(strategy_id: int | None, name: str, code: str) -> int:
    _ensure_tables()
    validate_code(code)
    now = _now()
    with store._conn() as conn:  # noqa: SLF001
        if strategy_id:
            cur = conn.execute(
                "UPDATE quant_strategies SET name=?, code=?, updated_at=? WHERE id=?",
                (name, code, now, strategy_id),
            )
            if cur.rowcount:
                return int(strategy_id)
        cur = conn.execute(
            "INSERT INTO quant_strategies(name, code, created_at, updated_at) VALUES(?,?,?,?)",
            (name, code, now, now),
        )
        return int(cur.lastrowid)


def strategy_list() -> list[dict]:
    _ensure_tables()
    with store._conn() as conn:  # noqa: SLF001
        rows = conn.execute("SELECT * FROM quant_strategies ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def strategy_get(strategy_id: int) -> dict | None:
    _ensure_tables()
    with store._conn() as conn:  # noqa: SLF001
        row = conn.execute("SELECT * FROM quant_strategies WHERE id=?", (strategy_id,)).fetchone()
    return dict(row) if row else None


def paper_start(strategy_id: int, account_id: int | None = None) -> dict:
    _ensure_tables()
    strat = strategy_get(strategy_id)
    if not strat:
        raise ValueError("strategy not found")
    now = _now()
    with store._conn() as conn:  # noqa: SLF001
        cur = conn.execute(
            "INSERT INTO quant_paper_accounts(strategy_id, created_at) VALUES(?,?)",
            (strategy_id, now),
        )
        return {"id": int(cur.lastrowid)}
