import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from datetime import datetime

from app.core.cache import TTLCache
from app.core.store import Store
from app.datasources.intraday import parse_session
from app.services import stock_intraday

RAW = ['0930 10.00 100 100000', '0931 10.10 150 150500', '1130 10.20 200 201500', '1300 10.20 200 201500', '1500 10.30 300 304500', '1530 10.30 400 407500']

class IntradayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Store(str(Path(self.tmp.name)/'db.sqlite'))
    def tearDown(self):
        self.tmp.cleanup()

    def test_cumulative_units_vwap_interval_and_after_hours(self):
        session=parse_session('20260909',RAW,'9.8')
        p=session['points']
        self.assertEqual(len(p),5)
        self.assertEqual(session['excluded_count'],1)
        self.assertEqual(session['quality_status'],'gaps')
        self.assertIn('09:32',session['missing_times'])
        self.assertNotIn('12:00',session['missing_times'])
        self.assertEqual(p[1]['volume'],5000)
        self.assertEqual(p[1]['amount'],50500)
        self.assertAlmostEqual(p[1]['average_price'],150500/15000,places=6)
        self.assertEqual(p[-1]['cumulative_volume'],30000)
        self.assertEqual(p[-1]['cumulative_amount'],304500)
        self.assertEqual(sum(r['volume'] for r in p),30000)
        self.assertEqual(p[3]['volume'],0)
        self.assertEqual(p[3]['interval_start'],'11:30')
        self.assertTrue(p[0]['first_interval_includes_opening_auction'])
        self.assertEqual(p[-1]['time'],'15:00')
        self.assertEqual(session['raw_rows'],RAW)

    def test_bad_data_and_index_vwap_not_invented(self):
        for rows in [['0930 10 100 100000','0931 10 99 100000'],['0930 10 100 100000']*2,['0930 NaN 100 100000'],['0930 10 100']]:
            with self.assertRaises(ValueError):parse_session('20260909',rows)
        result=parse_session('20260909',RAW,stock=False)
        self.assertIsNone(result['points'][0]['average_price'])
        self.assertIsNone(result['points'][0]['volume'])

    async def test_singleflight_archive_and_exact_day(self):
        times = [f'{m//60:02d}{m%60:02d}' for a,b in ((570,690),(780,900)) for m in range(a,b+1)]
        complete_rows = [f'{t} 10 100 100000' for t in times]
        raw={'data':{'sz000001':{'data':[{'date':'20260909','data':complete_rows,'prec':'9.8'}]}}}
        with patch.object(stock_intraday,'store',self.db),patch.object(stock_intraday,'cache',TTLCache()),patch.object(stock_intraday.tencent,'fetch_json',AsyncMock(return_value=raw)) as fetch:
            results=await asyncio.gather(*(stock_intraday.review('000001','2026-09-09') for _ in range(4)))
            self.assertEqual(fetch.await_count,1)
            self.assertEqual(results[0]['date'],'2026-09-09')
            self.assertEqual(len(results[0]['points']),242)
            self.assertNotIn('raw_rows',results[0])
            self.assertEqual(self.db.stock_intraday_get('sz000001','2026-09-09')['raw_rows'],complete_rows)
            fetch.side_effect=RuntimeError('unavailable')
            stock_intraday.cache.delete('stock_intraday_v1:sz000001')
            result=await stock_intraday.review('000001','2026-09-09')
            self.assertEqual(result['status'],'ok')
            self.assertEqual(fetch.await_count,1)  # complete history is offline-readable
            missing=await stock_intraday.review('000001','2026-09-08')
            self.assertEqual(missing['points'],[])
            self.assertEqual(missing['date'],'2026-09-08')
            self.assertEqual(missing['status'],'refresh_failed')

    def test_older_partial_cannot_erase_later_snapshot(self):
        complete=parse_session('20260909',RAW)
        partial=parse_session('20260909',RAW[:2])
        self.db.stock_intraday_save('sz000001',[complete])
        self.db.stock_intraday_save('sz000001',[partial])
        self.assertEqual(len(self.db.stock_intraday_get('sz000001','2026-09-09')['points']),5)

    async def test_legacy_mapping_preserves_index_identity(self):
        from app.datasources import tencent
        from app.services import market
        raw={'data':{'sz000001':{'data':{'date':'20260909','data':RAW}}}}
        with patch.object(tencent,'fetch_json',AsyncMock(return_value=raw)):
            rows=await tencent.trend_minute_by_symbol('sz000001')
        self.assertEqual(rows[1][3],5000)
        self.assertAlmostEqual(rows[1][2],150500/15000,places=6)
        with patch.object(market,'cache',TTLCache()),patch.object(tencent,'trend_minute_by_symbol',AsyncMock(return_value=[])) as fetch:
            await market.trend('000001')
        fetch.assert_awaited_once_with('sh000001')
