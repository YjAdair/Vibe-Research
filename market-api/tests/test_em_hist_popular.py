"""em_hist store + snapshot self-check."""
from __future__ import annotations
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.core.store import Store
from app.services import popular


class EmHistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'db.sqlite'))

    def tearDown(self):
        self.tmp.cleanup()

    def test_store_cover_and_ranks(self):
        self.db.em_popular_rank_save_rows([
            {'symbol_code': '600001', 'trade_date': '2026-09-10', 'rank': 20},
            {'symbol_code': '600001', 'trade_date': '2026-09-11', 'rank': 10},
        ])
        self.db.em_popular_rank_set_cover('600001', '2026-09-10', '2026-09-11', 't')
        self.assertEqual(self.db.em_popular_rank_uncovered(['600001', '600002'], '2026-09-11'), ['600002'])
        self.assertEqual(self.db.em_popular_rank_get_many('2026-09-11', ['600001']), {'600001': 10})
        self.assertEqual(self.db.em_popular_rank_prev('600001', '2026-09-11'), 20)

    def test_em_hist_snapshot_assembles(self):
        async def fake_hist(code, year_type='2'):
            return [
                {'trade_date': '2026-09-10', 'rank': 30, 'symbol_code': code},
                {'trade_date': '2026-09-11', 'rank': 5, 'symbol_code': code},
            ]

        with patch.object(popular, 'store', self.db), \
                patch('app.datasources.eastmoney.popularity_rank_history', AsyncMock(side_effect=fake_hist)):
            snap = asyncio.run(popular.em_hist_snapshot(['600001'], '2026-09-11'))
        self.assertEqual(snap['source'], 'em_hist')
        self.assertEqual(snap['items'][0]['rank'], 5)
        self.assertEqual(snap['items'][0]['rank_diff'], 25)


if __name__ == '__main__':
    unittest.main()
