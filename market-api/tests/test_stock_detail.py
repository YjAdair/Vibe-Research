import unittest
from unittest.mock import AsyncMock, patch
from app.core.cache import TTLCache
from app.services import stock_detail

class StockDetailTests(unittest.IsolatedAsyncioTestCase):
    async def test_stock_identity_historical_cutoff_units_and_no_live_quote(self):
        raw = {'data': {'sz000001': {'day':[
            ['2026-09-08','10','11','12','9','100'],
            ['2026-09-09','11','12','13','10','200'],
            ['2026-09-10','12','13','14','11','300']]}}}
        fetch = AsyncMock(return_value=raw)
        with patch.object(stock_detail,'cache',TTLCache()), patch.object(stock_detail.tencent,'fetch_json',fetch), patch.object(stock_detail.tencent,'realtime',AsyncMock()) as live:
            result = await stock_detail.detail('000001','2026-09-09',120)
        self.assertEqual(result['symbol'],'sz000001')
        self.assertEqual([b['date'] for b in result['bars']],['2026-09-08','2026-09-09'])
        self.assertEqual(result['bars'][0]['volume'],10000)
        self.assertIsNone(result['quote'])
        live.assert_not_awaited()
        self.assertTrue(fetch.call_args.kwargs['params']['param'].startswith('sz000001,'))

    async def test_adjusted_and_invalid_bars_not_mislabeled_unadjusted(self):
        for node in [{'qfqday':[['2026-09-09','1','2','3','1','20']]},
                     {'day':[['2026-09-09','10','11','9','12','20']]}]:
            with patch.object(stock_detail,'cache',TTLCache()), patch.object(stock_detail.tencent,'fetch_json',AsyncMock(return_value={'data':{'sz000001':node}})):
                result = await stock_detail.detail('000001','2026-09-09')
            self.assertEqual(result['bars'],[])
            self.assertEqual(result['status'],'partial')
