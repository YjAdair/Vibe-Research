import tempfile
import unittest
import json
from pathlib import Path

from app.core.store import Store
from app.datasources.ths_board import _parse_bars, _extract_js_payload, load_catalog


class ThsBoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'test.db'))

    def tearDown(self):
        self.tmp.cleanup()

    def test_catalog_loaded(self):
        catalog = load_catalog()
        self.assertGreater(len(catalog), 400)
        for entry in catalog[:50]:
            self.assertIn('code', entry)
            self.assertIn('name', entry)
            self.assertIn(entry['kind'], ('industry', 'concept'))
            self.assertTrue(entry['code'].startswith(('881', '885', '886')))

    def test_em_ths_board_map_integrity(self):
        """EM->THS 显式映射：结构完整、值域合法、corr 门槛。"""
        path = Path(__file__).resolve().parent.parent / 'app' / 'datasources' / 'em_ths_board_map.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        mappings = data['mappings']
        self.assertGreaterEqual(len(mappings), 150)
        catalog_names = {b['code'] for b in load_catalog()}
        for em_code, hit in mappings.items():
            self.assertRegex(em_code, r'^BK[0-9]+$')
            self.assertIn(hit['ths_code'], catalog_names, f'{em_code} -> {hit["ths_code"]} not in THS catalog')
            self.assertGreaterEqual(hit['corr'], 0.85)

    def test_parse_bars(self):
        payload = '20260910,10.5,11.0,10.2,10.8,1000,20000,,,,0;20260911,10.8,11.2,10.6,11.0,1200,24000,,,,0'
        bars = _parse_bars(payload)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0]['date'], '2026-09-10')
        self.assertEqual(bars[0]['close'], 10.8)
        self.assertEqual(bars[1]['volume'], 1200)
        # malformed rows are skipped
        self.assertEqual(_parse_bars('garbage,not,numbers'), [])

    def test_extract_js_payload(self):
        text = 'quotebridge_v4_line_bk_885710_01_last({"num":140,"name":"x","data":"1,2,3"})'
        data = _extract_js_payload(text)
        self.assertEqual(data['num'], 140)
        with self.assertRaises(ValueError):
            _extract_js_payload('not json at all')

    def test_store_roundtrip_and_stats(self):
        bars = [{'date': '2026-09-10', 'open': 1, 'high': 2, 'low': 0.5, 'close': 1.5, 'volume': 100, 'amount': 150},
                {'date': '2026-09-11', 'open': 1.5, 'high': 1.8, 'low': 1.4, 'close': 1.6, 'volume': 120, 'amount': 180}]
        n = self.db.ths_board_daily_save('885710', bars)
        self.assertEqual(n, 2)
        rows = self.db.ths_board_daily_range('885710', '2026-09-01', '2026-09-30')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]['date'], '2026-09-11')
        stats = {r['board_code']: r for r in self.db.ths_board_daily_stats()}
        self.assertEqual(stats['885710']['bars'], 2)
        self.assertEqual(stats['885710']['latest'], '2026-09-11')
        # upsert does not duplicate
        self.db.ths_board_daily_save('885710', bars)
        self.assertEqual(len(self.db.ths_board_daily_range('885710', '2026-09-01', '2026-09-30')), 2)


if __name__ == '__main__':
    unittest.main()
