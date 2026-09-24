import unittest

from app.datasources.ths import _high_days_lbc


class HighDaysLbcTest(unittest.TestCase):
    """lbc 是连续连板数，不是区间涨停板数（'5天3板' -> 1，不是 3）。"""

    def test_consecutive(self):
        self.assertEqual(_high_days_lbc('4天4板'), 4)
        self.assertEqual(_high_days_lbc('2天2板'), 2)
        self.assertEqual(_high_days_lbc('10天10板'), 10)

    def test_non_consecutive_is_one(self):
        self.assertEqual(_high_days_lbc('5天3板'), 1)
        self.assertEqual(_high_days_lbc('10天7板'), 1)
        self.assertEqual(_high_days_lbc('3天2板'), 1)

    def test_invalid(self):
        self.assertEqual(_high_days_lbc(''), 1)
        self.assertEqual(_high_days_lbc('x天y板'), 1)
        self.assertEqual(_high_days_lbc(None), 1)


if __name__ == '__main__':
    unittest.main()
