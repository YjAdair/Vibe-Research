"""梯队清洗：收盘封板校验 / 补漏 / L与区间分列。"""
import unittest

from app.services.ladder_clean import _desc_sealed, _is_close_sealed, _keep_times, build_ladder_rows
from app.services import plate_flow


class LadderCleanTests(unittest.TestCase):
    def test_desc_separates_interval_from_lianban(self):
        self.assertEqual(_desc_sealed(6, 4, '6天4板', 2), '6天4板')
        self.assertEqual(_desc_sealed(3, 3, '3天3板', 3), '3连板')
        self.assertEqual(_desc_sealed(0, 0, '首板', 1), '首板')
        self.assertEqual(_desc_sealed(0, 0, '', 2), '2连板')

    def test_keep_times_interval_uses_ct(self):
        # 原站 600815 3天2板 → keep=2，进 2 板档（不是 lbc=1）
        self.assertEqual(_keep_times(1, 3, 2), 2)
        self.assertEqual(_keep_times(4, 4, 4), 4)  # 连续 4 连板
        self.assertEqual(_keep_times(2, 2, 2), 2)
        self.assertEqual(_keep_times(1, 0, 0), 1)  # 首板
        self.assertEqual(_keep_times(0, 3, 3), 3)  # 缺 lbc 的连续板

    def test_close_seal_detect(self):
        q = {'prev_close': 14.84, 'close': 15.87, 'high': 16.32, 'stock_name': '中国长城'}
        self.assertIs(_is_close_sealed(q, '000066'), False)
        q2 = {'prev_close': 68.05, 'close': 74.86, 'high': 74.86, 'stock_name': '国恩股份'}
        self.assertIs(_is_close_sealed(q2, '002768'), True)

    def test_chip_20260922_reclass_and_supplement(self):
        members = plate_flow.plate_members_full('801001', '2026-09-22')
        built = build_ladder_rows('2026-09-22', members)
        sealed = {r['stock_code']: r for r in built['sealed']}
        broken = {r['stock_code']: r for r in built['broken']}
        self.assertIn('000066', broken)
        self.assertNotIn('000066', sealed)
        self.assertEqual(broken['000066']['up_limit_keep_times'], 0)
        self.assertIn('000066', built['meta']['reclassified_to_broken'])
        self.assertIn('002768', sealed)
        self.assertGreaterEqual(sealed['002768']['up_limit_keep_times'], 1)
        self.assertIn('002768', built['meta']['supplemented_close_seal'])
        for r in built['sealed']:
            self.assertGreaterEqual(r['up_limit_keep_times'], 1)


if __name__ == '__main__':
    unittest.main()
