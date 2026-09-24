"""晋级公式回归测试：次日涨停判定必须用精确涨停价，不能固定 9.5 阈值。"""
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.board_hot import _exact_limit_up_price  # noqa: E402


class TestExactLimitUpPrice(unittest.TestCase):
    def test_main_board_10_percent(self):
        self.assertEqual(_exact_limit_up_price(10.00, "600519", "贵州茅台"), 11.00)
        self.assertEqual(_exact_limit_up_price(5.55, "000001", "平安银行"), 6.11)

    def test_chinext_star_20_percent(self):
        self.assertEqual(_exact_limit_up_price(10.00, "300750", "宁德时代"), 12.00)
        self.assertEqual(_exact_limit_up_price(50.00, "688981", "中芯国际"), 60.00)

    def test_st_excluded(self):
        self.assertIsNone(_exact_limit_up_price(10.00, "600519", "ST某某"))
        self.assertIsNone(_exact_limit_up_price(10.00, "000001", "*ST某某"))

    def test_bj_excluded(self):
        self.assertIsNone(_exact_limit_up_price(10.00, "830799", "北交所股票"))
        self.assertIsNone(_exact_limit_up_price(10.00, "430047", "新三板股票"))

    def test_promotion_threshold_semantics(self):
        prev, code = 10.00, "300750"
        limit = _exact_limit_up_price(prev, code, "宁德时代")
        next_close_15pct = 11.50
        self.assertFalse(abs(next_close_15pct - limit) < 0.005)
        next_close_limit = 12.00
        self.assertTrue(abs(next_close_limit - limit) < 0.005)


if __name__ == "__main__":
    unittest.main()
