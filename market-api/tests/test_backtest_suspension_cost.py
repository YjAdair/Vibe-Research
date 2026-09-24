"""回测引擎：停牌日估值与卖出成本结转测试。"""
import sys
import os
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import quant_backtest  # noqa: E402


def _row(date, close, code="600519", name="贵州茅台"):
    return {
        "trade_date": date, "stock_code": code, "stock_name": name,
        "open": close, "high": close, "low": close,
        "close": close, "prev_close": close,
    }


SUSPENDED_CODE = """
def initialize(context):
    context.security = '600519'
    context.universe = ['600519', '000001']
def handle_data(context, data):
    if context.portfolio.cash > 500000:
        order(context.security, 100)
"""


class TestSuspensionValuation(unittest.TestCase):
    """停牌日持仓市值不得归零（曾造成假回撤）。

    day1：600519 @10 买入 100 股（另一只 000001 @5 只是占位，不交易）。
    day2：600519 停牌（无行情行），000001 正常交易——这一天会出现在 all_days。
    旧 bug：day2 的 600519 市值按 0 计，权益假跌 1000；修复后沿用 day1 收盘价 10。
    """

    def test_suspended_day_uses_last_known_close(self):
        hist = {
            "600519": [_row("2026-07-01", 10.0), _row("2026-07-03", 10.0)],
            "000001": [_row("2026-07-01", 5.0, "000001", "平安银行"),
                       _row("2026-07-02", 5.0, "000001", "平安银行"),
                       _row("2026-07-03", 5.0, "000001", "平安银行")],
        }
        with mock.patch.object(quant_backtest, "_load_history", return_value=hist):
            result = quant_backtest.run_backtest(SUSPENDED_CODE, "2026-07-01", "2026-07-03", 1_000_000.0)
        curve = {e["date"]: e["equity"] for e in result["equity_curve"]}
        self.assertEqual(sorted(curve), ["2026-07-01", "2026-07-02", "2026-07-03"])
        # day1：100 股 @10 成交，收盘估值权益不变
        self.assertEqual(curve["2026-07-01"], 1000000.0)
        # day2 停牌：沿用 10 元估值，权益不得出现假回撤
        self.assertEqual(curve["2026-07-02"], 1000000.0,
                         f"suspension must not create phantom drawdown: {curve}")
        self.assertEqual(curve["2026-07-03"], 1000000.0)


class TestSellCostCarryover(unittest.TestCase):
    """卖出成本按比例结转：分批买卖后 cost 不失真。

    day1 买 100 @10（cost 1000），day2 买 100 @20（cost 3000，均价 15），
    day3 卖 100 @30：剩余 100 股 cost 应为 1500（按比例结转），
    而旧实现按卖出价扣减会得到 3000 - 3000 = 0。
    """

    def test_partial_sell_cost_proportional(self):
        code = """
def initialize(context):
    context.security = '600519'
    context.universe = ['600519']
    context.day_count = 0
def handle_data(context, data):
    context.day_count += 1
    if context.day_count == 1:
        order(context.security, 100)
    elif context.day_count == 2:
        order(context.security, 100)
    elif context.day_count == 3:
        order(context.security, -100)
    elif context.day_count == 4:
        log.info(str(context.portfolio.positions['600519']['cost']))
"""
        hist = {"600519": [
            _row("2026-07-01", 10.0), _row("2026-07-02", 20.0),
            _row("2026-07-03", 30.0), _row("2026-07-04", 40.0),
        ]}
        with mock.patch.object(quant_backtest, "_load_history", return_value=hist):
            result = quant_backtest.run_backtest(code, "2026-07-01", "2026-07-04", 1_000_000.0)
        # 从日志里取出 day4 打印的持仓成本（log.info 输出为纯数字）
        cost_logs = [x for x in result["logs"] if x.replace(".", "", 1).isdigit()]
        self.assertTrue(cost_logs, f"expected cost log in {result['logs']}")
        self.assertEqual(float(cost_logs[-1]), 1500.0,
                         f"cost after partial sell should be 1500, got {cost_logs[-1]}")
        # 卖出后现金也应正确：100 万 - 1000 - 2000 + 3000 = 100 万
        self.assertEqual(result["equity_curve"][-1]["equity"], 1000000.0 + 100 * 40.0,
                         f"final equity should be cash 1,000,000 + 100 shares @40: {result['equity_curve'][-1]}")


if __name__ == "__main__":
    unittest.main()
