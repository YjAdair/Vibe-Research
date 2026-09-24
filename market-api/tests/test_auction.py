import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.core.cache import TTLCache
from app.core.store import Store
from app.datasources.auction import TZ, extract, limit_buy_from_quote
from app.services import auction


def clock(t='09:25:30'):
    return datetime.fromisoformat('2026-09-10T' + t).replace(tzinfo=TZ)


def quote(stamp='20260910092525'):
    return {'code':'600001', 'open':11, 'prev_close':10, 'amount':1200, 'turnover':.2, 'timestamp':stamp}


class AuctionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'db.sqlite'))

    def test_limit_buy_from_quote(self):
        base = {'price': 11.0, 'up_px': 11.0, 'bid_grp': '11,1000,0,10.9,50,0,'}
        self.assertEqual(limit_buy_from_quote(base), 11.0 * 1000 * 100)
        self.assertIsNone(limit_buy_from_quote({**base, 'price': 10.5}))          # not at limit
        self.assertIsNone(limit_buy_from_quote({**base, 'up_px': 0.0}))           # no limit price
        self.assertIsNone(limit_buy_from_quote({**base, 'bid_grp': '10.9,100,0,'}))  # bid1 below limit
        self.assertIsNone(limit_buy_from_quote({**base, 'bid_grp': ''}))          # missing book
        row = extract({**quote(), **base, 'timestamp': '20260910092530'}, '2026-09-10', clock())
        self.assertEqual(row['up_limit_buy_amount'], 11.0 * 1000 * 100)
        self.assertIn('bid1 unmatched hands', row['field_sources']['up_limit_buy_amount']['basis'])

    def tearDown(self):
        self.tmp.cleanup()

    def test_phase_boundaries_and_units(self):
        q = extract(quote(), '2026-09-10', clock())
        self.assertEqual(q['auction_amount'], 12e6)
        self.assertEqual(q['auction_pct'], 10)
        self.assertIsNone(q['daily_trade_amount'])
        self.assertIsNone(q['up_limit_buy_amount'])
        after = extract(quote('20260910150001'), '2026-09-10', clock('16:00:00'))
        self.assertIsNone(after['auction_amount'])
        self.assertIsNone(after['auction_turnover'])
        self.assertEqual(after['daily_trade_amount'], 12e6)
        continuous = extract(quote('20260910093000'), '2026-09-10', clock('09:30:05'))
        self.assertIsNone(continuous['auction_amount'])
        self.assertIsNone(extract(quote('20260910092459'), '2026-09-10', clock()))

    def test_wrong_day_future_stale_and_invalid_quotes(self):
        for stamp in ['20260909092525', '20260911092525', 'bad', '20260910092600']:
            self.assertIsNone(extract(quote(stamp), '2026-09-10', clock()))
        self.assertIsNone(extract({**quote(), 'prev_close':0}, '2026-09-10', clock()))
        invalid = extract({**quote(), 'field_validity': {'amount':False,'turnover':False}}, '2026-09-10', clock())
        self.assertIsNone(invalid['auction_amount'])
        self.assertIsNone(invalid['auction_turnover'])
        stale = extract(quote('20260910092500'), '2026-09-10', clock('09:29:00'))
        self.assertIsNone(stale['auction_amount'])
        after = extract(quote(), '2026-09-10', clock('15:00:00'))
        self.assertIsNone(after['auction_amount'])

    def test_real_signals_and_unknown_rules(self):
        rows = [dict(up_limit_buy_amount=300,auction_pct=8,auction_amount=200e6,daily_trade_amount=2e9),
                dict(up_limit_buy_amount=100,auction_pct=4,auction_amount=90e6,daily_trade_amount=1e9),
                dict(daily_trade_amount=600e6)]
        result = auction.signals(rows)[0]
        self.assertEqual(result['signal'], '1,2,3')
        self.assertTrue(result['signal_complete'])
        self.assertTrue(result['volume_increase'])
        rows[0]['up_limit_buy_amount'] = None
        result = auction.signals(rows)[0]
        self.assertEqual(result['signal'], '2,3')
        self.assertIsNone(result['signal_rules']['1'])
        self.assertFalse(result['signal_complete'])
        empty = auction.signals([{}, {}, {}])[0]
        self.assertEqual(empty['signal'], 0)
        self.assertEqual(list(empty['signal_rules'].values()), [None, None, None])
        rows[0]['auction_pct'] = 6  # exactly 50%: rule1 must not fire
        rows[0]['up_limit_buy_amount'] = 300
        self.assertFalse(auction.signals(rows)[0]['signal_rules']['1'])

    def test_after_close_does_not_erase_auction_and_evidence_idempotent(self):
        opening = extract(quote(), '2026-09-10', clock())
        evidence = {'date':'2026-09-10', 'collected_at':clock().isoformat(), 'quote':quote()}
        self.db.auction_save([opening], [evidence])
        closing = extract(quote('20260910150000'), '2026-09-10', clock('16:00:00'))
        self.db.auction_save([closing], [evidence])
        row = self.db.auction_range('2026-09-10','2026-09-10')[0]
        self.assertEqual(row['auction_amount'], 12e6)
        self.assertIn('09:25:25',row['field_sources']['auction_amount']['source_as_of'])
        self.assertEqual(row['daily_trade_amount'], 12e6)
        with self.db._conn() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM auction_evidence').fetchone()[0],1)

    async def test_historical_read_never_uses_todays_quotes(self):
        with patch.object(auction,'store',self.db), patch.object(auction,'sessions',AsyncMock(return_value=['2026-09-08','2026-09-09'])), patch.object(auction,'collect',AsyncMock()) as collect:
            result = await auction.review('2026-09-09')
        collect.assert_not_awaited()
        self.assertEqual(result['status'],'missing')
        self.assertEqual(result['ladder'],[])
        self.assertEqual(result['date'],'2026-09-09')

    async def test_singleflight_and_partial_source_failure(self):
        stocks = [{'stock_code':'600001'}]
        with patch.object(auction,'store',self.db), patch.object(auction,'cache',TTLCache()), patch.object(auction,'now_local',return_value=clock()), patch.object(auction,'candidates',AsyncMock(return_value={'stocks':stocks})), patch.object(auction.tencent,'realtime',AsyncMock(return_value={'600001':quote()})) as fetch:
            result = await asyncio.gather(*(auction.collect('2026-09-10',['2026-09-09','2026-09-10']) for _ in range(5)))
            self.assertEqual(fetch.await_count,1)
            self.assertEqual(result[0]['auction_amount_count'],1)
            auction.cache.delete('auction_collect_v1:2026-09-10')
            fetch.side_effect = RuntimeError('upstream')
            failed = await auction.collect('2026-09-10',['2026-09-09','2026-09-10'])
            self.assertEqual(failed['status'],'partial')
            self.assertEqual(failed['received'],0)
            self.assertEqual(len(self.db.auction_range('2026-09-10','2026-09-10')),1)

    async def test_api_dates_bounds_and_aliases(self):
        from app.main import app
        with patch.object(auction,'review',AsyncMock(return_value={'date':'2026-09-09'})) as review:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                for query in ['date1=bad','date1=2026-02-30','days=11','date1=2099-01-01','date1=20260909&date=20260908']:
                    response = await client.get('/v3/open/review/dingpan/auction?' + query)
                    self.assertEqual(response.status_code,422,query)
                response = await client.get('/v3/open/review/dingpan/auction?date1=20260909&days=3')
                self.assertEqual(response.status_code,200)
                review.assert_awaited_once_with('2026-09-09',3)
