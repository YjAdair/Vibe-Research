"""量化编辑器回测引擎测试。"""
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import quant_backtest  # noqa: E402


GOOD = """
def initialize(context):
    context.security = '600519'
    context.universe = ['600519']
def handle_data(context, data):
    if context.portfolio.cash > 500000:
        order(context.security, 100)
"""

NO_INIT = """
def handle_data(context, data):
    pass
"""

NO_SEC = """
def initialize(context):
    pass
def handle_data(context, data):
    pass
"""

NO_DATA = """
def initialize(context):
    context.security = '999999'
def handle_data(context, data):
    pass
"""

DANGEROUS = """
import os
def initialize(context):
    context.security = '600519'
def handle_data(context, data):
    pass
"""


class TestQuantBacktest(unittest.TestCase):
    def test_missing_initialize(self):
        with self.assertRaises(ValueError) as cm:
            quant_backtest.run_backtest(NO_INIT, "2026-07-01", "2026-09-11")
        self.assertIn("Missing initialize or handle_data", str(cm.exception))

    def test_missing_security(self):
        with self.assertRaises(ValueError) as cm:
            quant_backtest.run_backtest(NO_SEC, "2026-07-01", "2026-09-11")
        self.assertIn("No securities defined", str(cm.exception))

    def test_no_data(self):
        with self.assertRaises(ValueError) as cm:
            quant_backtest.run_backtest(NO_DATA, "2026-07-01", "2026-09-11")
        self.assertIn("No data loaded for any security", str(cm.exception))

    def test_dangerous_code_rejected(self):
        with self.assertRaises(ValueError) as cm:
            quant_backtest.run_backtest(DANGEROUS, "2026-07-01", "2026-09-11")
        self.assertIn("rejected", str(cm.exception).lower())

    def test_good_strategy(self):
        # 先看库里有没有 600519 的数据，没有就跳过
        hist = quant_backtest._load_history(["600519"], "2026-07-01", "2026-09-11")
        if not any(hist.values()):
            self.skipTest("no daily_close data for 600519 in test db")
        result = quant_backtest.run_backtest(GOOD, "2026-07-01", "2026-09-11")
        self.assertIn("metrics", result)
        self.assertIn("equity_curve", result)
        self.assertIn("logs", result)
        self.assertGreater(len(result["equity_curve"]), 0)
        for m in ("total_return", "annual_return", "max_drawdown", "sharpe_ratio", "volatility"):
            self.assertIn(m, result["metrics"])

    def test_strategy_save_list(self):
        sid = quant_backtest.strategy_save(None, "test-strategy", GOOD)
        self.assertGreater(sid, 0)
        rows = quant_backtest.strategy_list()
        hit = [r for r in rows if r["id"] == sid]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["name"], "test-strategy")


if __name__ == "__main__":
    unittest.main()


T_PLUS_1 = """
def initialize(context):
    context.security = '600519'
    context.day_count = 0
def handle_data(context, data):
    context.day_count += 1
    if context.day_count == 1:
        order(context.security, 100)
        order(context.security, -100)  # 当日卖，应被 T+1 拒绝
    elif context.day_count == 2:
        order(context.security, -100)  # 次日卖，应成功
"""


class TestTPlusOneRule(unittest.TestCase):
    def test_same_day_sell_rejected(self):
        hist = quant_backtest._load_history(["600519"], "2026-07-01", "2026-09-11")
        if not any(hist.values()):
            self.skipTest("no daily_close data for 600519 in test db")
        result = quant_backtest.run_backtest(T_PLUS_1, "2026-07-01", "2026-09-11")
        logs = result["logs"]
        self.assertTrue(any("T+1 rule" in str(x) for x in logs), f"expected T+1 rejection in logs: {logs[:5]}")
        # 次日卖出成功后持仓应为 0：终值 = 现金（卖出所得）
        # 无法直接断言 positions，但 logs 不应再出现第二次 T+1 拒绝
        self.assertEqual(sum(1 for x in logs if "T+1 rule" in str(x)), 1)


if __name__ == "__main__":
    unittest.main()


SELL_FLOW = """
def initialize(context):
    context.security = '600519'
    context.day_count = 0
def handle_data(context, data):
    context.day_count += 1
    if context.day_count == 1:
        order(context.security, 100)
    elif context.day_count == 2:
        order(context.security, -100)  # 次日卖出必须成功（曾因 qty<=0 拦截负数卖单而失效）
"""


class TestSellFlow(unittest.TestCase):
    def test_next_day_sell_executes(self):
        hist = quant_backtest._load_history(["600519"], "2026-07-01", "2026-09-11")
        if not any(hist.values()):
            self.skipTest("no daily_close data for 600519 in test db")
        result = quant_backtest.run_backtest(SELL_FLOW, "2026-07-01", "2026-09-11")
        # 全程只有 day1 买入 day2 卖出：之后权益应恒等于现金（清仓状态）
        curve = result["equity_curve"]
        if len(curve) > 2:
            self.assertEqual(curve[-1]["equity"], curve[2]["equity"],
                             f"equity should stay flat after full close: {curve[:4]}")
        # 不应出现 no data / rejected 日志
        self.assertFalse(any("rejected" in str(x) or "no data" in str(x) for x in result["logs"]),
                         f"unexpected rejection logs: {result['logs'][:5]}")


if __name__ == "__main__":
    unittest.main()
