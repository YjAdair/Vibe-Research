"""择时公式单测: _market_score 分档边界 + _timing_flags 与前端 MA10 口径一致。"""
import unittest

from app.services.timing import _market_score, _timing_flags


class MarketScoreTests(unittest.TestCase):
    def test_extreme_bull(self):
        # 涨停80/高度10/涨>5%达800/强度拉满 => 100
        self.assertEqual(_market_score(80, 10, 800, 0.5, 0.0), 100.0)

    def test_extreme_bear(self):
        # 无涨停/无高度/无赚钱效应/炸板吃掉强度 => 0
        self.assertEqual(_market_score(0, 0, 0, 0.0, 0.3), 0.0)

    def test_partial(self):
        # 涨停40(20分) 高度5(12.5分) 涨>5% 400(10分) 强度0(0分)
        self.assertEqual(_market_score(40, 5, 400, 0.0, 0.0), 42.5)

    def test_strength_negative_clamped(self):
        # 炸板率高于上涨占比时强度为负, 截断到0, 不倒扣
        self.assertEqual(_market_score(40, 5, 400, 0.1, 0.5), 42.5)


class TimingFlagsTests(unittest.TestCase):
    def test_ma10_alignment_with_frontend(self):
        # 前9点无论多高都不触发区间(前端 MA10 前9点为 null)
        flags = _timing_flags([200.0] * 12, bull_line=100.0)
        self.assertEqual(flags[:9], [0] * 9)
        self.assertEqual(flags[9], -1)
        self.assertEqual(flags[10:], [1] * 2)

    def test_close_on_fall_below_line(self):
        # 从第10点起连续高位, 连续下跌拉低 MA10 跌破线 => 起点前一日-1, 跌破日0, 区间关闭
        scores = [150.0] * 12 + [10.0] * 4
        flags = _timing_flags(scores, bull_line=100.0)
        self.assertEqual(flags[9], -1)
        self.assertEqual(flags[13], 1)
        self.assertEqual(flags[14], 1)  # MA10=108 仍在线上
        self.assertEqual(flags[15], 0)  # MA10=94 跌破, 区间关闭

    def test_short_series_no_flags(self):
        self.assertEqual(_timing_flags([300.0] * 8), [0] * 8)


if __name__ == '__main__':
    unittest.main()
