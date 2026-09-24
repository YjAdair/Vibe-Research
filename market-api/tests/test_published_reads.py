import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from datetime import datetime

from app.config import settings
from app.core.store import Store
from app.services import pools, sentiment, topic
from app.datasources import eastmoney, ths


class PublishedReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'db.sqlite'))
        self.mode = settings.collector_mode
        settings.collector_mode = 'external'

    def tearDown(self):
        settings.collector_mode = self.mode
        self.tmp.cleanup()

    def payload(self, kind='up', date='2026-09-10', rows=None):
        rows = rows or [{'c': '000001', 'n': '平安银行', 'zdp': 10, 'lbc': 1, 'hybk': '银行', 'p': 10000, 'amount': 1}]
        payload={'pool_kind': kind, 'source': 'eastmoney', 'date': date, 'requested_date': date,
                'total': len(rows), 'pool': rows, 'complete': True, 'collected_at': date+'T15:00:00+08:00', 'empty_ok': True}
        if kind=='up':
            payload['concepts_by_code']={r['c']: [r.get('hybk') or '银行'] for r in rows if r.get('c')}
        return payload

    async def test_truncated_pool_is_not_published(self):
        with patch.object(pools, 'store', self.db), patch.object(eastmoney, 'limit_up_pool', AsyncMock(return_value={'total': 2, 'qdate': '20260910', 'pool': [{'c': '000001'}]})):
            with self.assertRaises(ValueError):
                await pools.collect_eastmoney('up', '2026-09-10')
        self.assertIsNone(self.db.limit_pool_get('em_up', '2026-09-10'))

    async def test_ths_pagination_and_duplicate_rejection(self):
        pages = {1: {'total': 2, 'pool': [{'code': '000001'}]}, 2: {'total': 2, 'pool': [{'code': '000002'}]}}
        fetch = AsyncMock(side_effect=lambda date, page, size: pages[page])
        with patch.object(pools, 'store', self.db), patch.object(ths, 'limit_up_pool', fetch):
            data = await pools.collect_ths('2026-09-10')
        self.assertEqual(data['total'], 2)
        pages[2] = {'total': 2, 'pool': [{'code': '000001'}]}
        with patch.object(pools, 'store', self.db), patch.object(ths, 'limit_up_pool', AsyncMock(side_effect=lambda date, page, size: pages[page])):
            with self.assertRaises(ValueError):
                await pools.collect_ths('2026-09-10')

    async def test_sentiment_and_review_do_not_hit_upstream(self):
        self.db.limit_pool_save('em_up', '2026-09-10', self.payload())
        self.db.limit_pool_save('em_down', '2026-09-10', self.payload('down', rows=[]))
        from app.core.cache import TTLCache
        with patch.object(pools, 'store', self.db), patch.object(eastmoney, 'limit_up_pool', AsyncMock()) as up, patch.object(eastmoney, 'limit_down_pool', AsyncMock()) as down, patch.object(sentiment, 'cache', TTLCache()):
            result = await sentiment.sentiment_today()
            up.assert_not_awaited(); down.assert_not_awaited()
        self.assertEqual(result['info'][0]['ztjs'], '1')
        self.assertEqual(result['source'], 'published_limit_pools')

    async def test_missing_requested_date_does_not_fallback_silently(self):
        self.db.limit_pool_save('em_up', '2026-09-10', self.payload())
        from app.main import app
        import httpx
        with patch.object(pools, 'store', self.db), patch.object(eastmoney, 'limit_up_pool', AsyncMock()) as up:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                data = (await client.get('/v3/open/review/uplimit/hot?date1=2026-09-09')).json()['data']
            up.assert_not_awaited()
        self.assertEqual(data['status'], 'missing')
        self.assertEqual(data['items'], [])

    async def test_topic_home_uses_published_snapshots_without_stock_board_calls(self):
        self.db.upsert_topic({'unique_key': 'T1', 'name': '液冷产业链(260825)', 'rows': [
            {'个股': '平安银行', '股票代码': '000001'},
            {'个股': '远东股份', '股票代码': '600869'},
            {'个股': '中兴通讯', '股票代码': '000063'},
        ]})
        self.db.daily_close_save('2026-09-10', [
            {'stock_code': '000001', 'stock_name': '平安银行', 'close': 11.0, 'prev_close': 10.0},
            {'stock_code': '600869', 'stock_name': '远东股份', 'close': 20.0, 'prev_close': 19.0},
            {'stock_code': '000063', 'stock_name': '中兴通讯', 'close': 30.0, 'prev_close': 31.0},
        ], metadata={'complete': True})
        from app.core.cache import TTLCache
        frozen = datetime(2026, 9, 10, 15, 0)
        with patch.object(topic, 'store', self.db), patch.object(topic, 'cache', TTLCache()), patch.object(topic, 'datetime') as dt, patch.object(eastmoney, 'stock_boards', AsyncMock()) as boards_call, patch.object(eastmoney, 'board_list', AsyncMock()) as board_list:
            dt.now.return_value = frozen
            dt.now.strftime = frozen.strftime
            result = await topic.home_topics(limit=10)
        boards_call.assert_not_awaited(); board_list.assert_not_awaited()
        item = result['items'][0]
        self.assertEqual(item['up_count'], 2)
        self.assertEqual(item['total_count'], 3)
        self.assertAlmostEqual(item['today_pct'], 4.01, delta=0.03)
        self.assertEqual(result['source'], 'topic_tables+daily_close')

class HomeIndexAndPanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'db.sqlite'))
        self.mode = settings.collector_mode
        settings.collector_mode = 'external'

    def tearDown(self):
        settings.collector_mode = self.mode
        self.tmp.cleanup()

    async def test_index_review_does_not_hit_upstream(self):
        from app.services import index_feed, market
        from app.datasources import tencent
        payload = {'date':'2026-09-10','slot':'15:00','complete':True,'total':1,'items':[
            {'code':'000001.SS','symbol':'sh000001','name':'上证指数','last_px':1,'preclose_px':1,'trend':[{'time':'1500','price':1}]}
        ]}
        self.db.index_snapshot_save('2026-09-10','15:00', payload)
        with patch.object(index_feed, 'store', self.db), patch.object(tencent, 'realtime_by_symbol', AsyncMock()) as rt, patch.object(tencent, 'trend_minute_by_symbol', AsyncMock()) as trend:
            rows = await market.index_trends()
        rt.assert_not_awaited(); trend.assert_not_awaited()
        self.assertEqual(rows[0]['name'], '上证指数')

    async def test_incomplete_index_batch_not_published(self):
        from app.services import index_feed
        from app.datasources import tencent
        from app.services.market import HOME_INDICES
        quotes = {idx['symbol'][2:]: {'timestamp':'20260910150000','price':1,'prev_close':1} for idx in HOME_INDICES}
        async def trend(symbol):
            if symbol.endswith('000001'):
                return []
            return [['1500', 1, None, None, 0]]
        with patch.object(index_feed, 'store', self.db), patch.object(tencent, 'realtime_by_symbol', AsyncMock(return_value=quotes)), patch.object(tencent, 'trend_minute_by_symbol', AsyncMock(side_effect=RuntimeError('missing'))):
            with self.assertRaises(ValueError):
                await index_feed.collect('2026-09-10')
        self.assertIsNone(self.db.index_snapshot_get('2026-09-10'))

    async def test_sentiment_panel_uses_published_pools_without_kline_scan(self):
        from app.services import intraday
        from app.datasources import eastmoney, tencent
        from app.core.cache import TTLCache
        self.db.limit_pool_save('em_up','2026-09-10', {'pool_kind':'up','source':'eastmoney','date':'2026-09-10','requested_date':'2026-09-10','total':1,'complete':True,'pool':[{'c':'000001','n':'平安银行','lbc':2}], 'empty_ok':True})
        self.db.limit_pool_save('em_down','2026-09-10', {'pool_kind':'down','source':'eastmoney','date':'2026-09-10','requested_date':'2026-09-10','total':0,'complete':True,'pool':[], 'empty_ok':True})
        self.db.minute_save('2026-09-10','15:00:00', {'date':'2026-09-10','minute':'15:00:00','up_num':1,'down_num':1,'zb_num':3,'fb_num':1,'uplimit_num':1,'gt_5_7_num':0,'gt_10_20_num':0,'lt_7_10_num':0,'lt_10_20_num':0})
        with patch.object(intraday, 'store', self.db), patch.object(pools, 'store', self.db), patch.object(eastmoney, 'limit_up_pool', AsyncMock()) as up, patch.object(eastmoney, 'market_snapshot', AsyncMock()) as snap, patch.object(tencent, 'kline_day', AsyncMock()) as kline, patch.object(tencent, 'kline_day_by_symbol', AsyncMock()) as idx:
            row = (await intraday.sentiment_data('2026-09-10'))[0]
        up.assert_not_awaited(); snap.assert_not_awaited(); kline.assert_not_awaited(); idx.assert_not_awaited()
        self.assertEqual(row['uplimit_num'], 1)
        self.assertEqual(row['zb_num'], 3)
        self.assertIsNone(row['bigleg_num'])

class ReviewHotAndLhbTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'db.sqlite'))
        self.mode = settings.collector_mode
        settings.collector_mode = 'external'

    def tearDown(self):
        settings.collector_mode = self.mode
        self.tmp.cleanup()

    async def test_board_hot_reads_published_pools_without_upstream(self):
        from app.services import board_hot
        from app.datasources import eastmoney, ths, tencent
        self.db.limit_pool_save('em_up','2026-09-10', {'pool_kind':'up','source':'eastmoney','date':'2026-09-10','requested_date':'2026-09-10','total':1,'complete':True,'empty_ok':True,'pool':[{'c':'000001','n':'平安银行','lbc':2,'hybk':'银行','fbt':92500,'amount':1,'fund':1}], 'concepts_by_code':{'000001':['银行','券商概念']}})
        self.db.limit_pool_save('em_broken','2026-09-10', {'pool_kind':'broken','source':'eastmoney','date':'2026-09-10','requested_date':'2026-09-10','total':1,'complete':True,'empty_ok':True,'pool':[{'c':'000002','n':'万科A','hybk':'银行'}]})
        self.db.limit_pool_save('ths_up','2026-09-10', {'pool_kind':'up','source':'10jqka','date':'2026-09-10','requested_date':'2026-09-10','total':1,'complete':True,'empty_ok':True,'pool':[{'code':'000001','reason':'业绩','high_days':'2天2板','open_num':0,'first_limit_up_time':'09:25'}]})
        self.db.board_snapshot_save(3,'2026-09-10', [{'plate_code':'BK0001','plate_name':'券商概念'}], {'trade_date':'2026-09-10'})
        with patch.object(board_hot, 'store', self.db), patch.object(pools, 'store', self.db), patch.object(eastmoney, 'limit_up_pool', AsyncMock()) as up, patch.object(eastmoney, 'stock_boards', AsyncMock()) as boards, patch.object(ths, 'block_top', AsyncMock()) as block, patch.object(tencent, 'kline_day', AsyncMock()) as kline:
            data = await board_hot.review('2026-09-10', 'concept')
        up.assert_not_awaited(); boards.assert_not_awaited(); block.assert_not_awaited(); kline.assert_not_awaited()
        self.assertEqual(data['status'], 'ok')
        names = [p[0] for p in data['plate']]
        self.assertIn('券商概念', names)
        self.assertIn('银行', names)
        self.assertEqual(data['plate_stocks']['BK0001'][0]['reason_info'], '业绩')
        self.assertEqual(data['plate_stocks_zb'], {})
        industry = await board_hot.review('2026-09-10', 'industry')
        zb_types = [row['up_limit_type'] for rows in industry['plate_stocks_zb'].values() for row in rows]
        self.assertIn('炸', zb_types)

    async def test_lhb_truncated_not_published_and_read_miss_is_empty(self):
        from app.services import lhb
        from app.datasources import eastmoney
        async def truncated(date, page=1, size=200):
            return {'pages': 2, 'data': [{'SECURITY_CODE':'000001'}] if page == 1 else []}
        with patch.object(lhb, 'store', self.db), patch.object(eastmoney, 'lhb_detail', truncated):
            with self.assertRaises(ValueError):
                await lhb.collect('2026-09-10')
        self.assertIsNone(self.db.lhb_get('2026-09-10'))
        with patch.object(lhb, 'store', self.db), patch.object(eastmoney, 'lhb_detail', AsyncMock()) as fetch:
            self.assertEqual(await lhb.lhb_list('2026-09-10'), [])
            fetch.assert_not_awaited()

class TierReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'db.sqlite'))
        self.mode = settings.collector_mode
        settings.collector_mode = 'external'

    def tearDown(self):
        settings.collector_mode = self.mode
        self.tmp.cleanup()

    async def test_pct_tier_missing_close_does_not_scan_kline(self):
        from app.services import uplimit
        from app.datasources import eastmoney
        from app.core.cache import TTLCache
        with patch.object(uplimit, 'cache', TTLCache()), patch.object(eastmoney, 'limit_up_pool', AsyncMock()) as up, patch.object(uplimit, '_pct_tier_from_store', AsyncMock(return_value=None)):
            data = await uplimit.pct_tier(10, '2026-09-10')
        up.assert_not_awaited()
        self.assertEqual(data['status'], 'missing')

    async def test_limit_up_tier_uses_published_pool(self):
        from app.main import app
        import httpx
        self.db.limit_pool_save('em_up','2026-09-10', {'pool_kind':'up','source':'eastmoney','date':'2026-09-10','requested_date':'2026-09-10','total':2,'complete':True,'empty_ok':True,'pool':[
            {'c':'000001','n':'平安银行','lbc':2,'p':10000,'zdp':10,'fbt':92500,'fund':1,'hybk':'银行'},
            {'c':'000002','n':'万科A','lbc':1,'p':20000,'zdp':9,'fbt':93000,'fund':2,'hybk':'地产'},
        ]})
        from app.datasources import ths, eastmoney
        with patch.object(pools, 'store', self.db), patch.object(ths, 'continuous_limit_up', AsyncMock()) as ths_call, patch.object(eastmoney, 'limit_up_pool', AsyncMock()) as up:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                data = (await client.get('/v3/market/limit-up-tier?date1=2026-09-10')).json()['data']
        ths_call.assert_not_awaited(); up.assert_not_awaited()
        self.assertEqual(data['source'], 'published_limit_pools')
        self.assertEqual(data['tiers'][0]['height'], 2)

class TimingAndZhabanReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'db.sqlite'))
        self.mode = settings.collector_mode
        settings.collector_mode = 'external'

    def tearDown(self):
        settings.collector_mode = self.mode
        self.tmp.cleanup()

    async def test_zhaban_chart_does_not_scan_market(self):
        from app.main import app
        import httpx
        from app.datasources import eastmoney, tencent
        self.db.limit_pool_save('em_up','2026-09-10', {'pool_kind':'up','source':'eastmoney','date':'2026-09-10','requested_date':'2026-09-10','total':2,'complete':True,'empty_ok':True,'pool':[{'c':'000001','lbc':1},{'c':'000002','lbc':2}]})
        self.db.limit_pool_save('em_broken','2026-09-10', {'pool_kind':'broken','source':'eastmoney','date':'2026-09-10','requested_date':'2026-09-10','total':1,'complete':True,'empty_ok':True,'pool':[{'c':'000003'}]})
        with patch.object(pools, 'store', self.db), patch.object(eastmoney, 'market_snapshot', AsyncMock()) as snap, patch.object(tencent, 'kline_day', AsyncMock()) as kline:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                data = (await client.get('/v3/sentiment/market/data?date1=2026-09-10')).json()['data']
        snap.assert_not_awaited(); kline.assert_not_awaited()
        self.assertEqual(data['source'], 'published_limit_pools')
        self.assertIn(2, data['uplimit_num'])
        self.assertIn(1, data['zb_num'])
        # 炸板率口径：炸板数 / (涨停总数 + 炸板数)，分母不是首板数
        i = data['categoryData'].index('2026-09-10')
        self.assertEqual(data['uplimit_n_num'][i], 1)
        self.assertEqual(data['zb_pct'][i], round(1 / (1 + 2) * 100, 2))

    async def test_timing_style2_does_not_fetch_index_kline(self):
        from app.services import timing
        from app.datasources import tencent
        from app.core.cache import TTLCache
        with patch.object(timing, 'cache', TTLCache()), patch.object(timing, 'store', self.db), patch.object(pools, 'store', self.db), patch.object(tencent, 'kline_day_by_symbol', AsyncMock()) as kline, patch('app.services.timing.trade_days', AsyncMock(return_value=['20260910'])):
            rows = await timing.market_style2('2026-09-10', 1)
        kline.assert_not_awaited()
        self.assertEqual(rows[0]['date'], '2026-09-10')

class CalendarReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'db.sqlite'))
        self.mode = settings.collector_mode
        settings.collector_mode = 'external'

    def tearDown(self):
        settings.collector_mode = self.mode
        self.tmp.cleanup()

    async def test_trade_days_uses_published_calendar(self):
        from app.services import market
        from app.datasources import tencent, sina
        from app.core.cache import TTLCache
        self.db.kv_set('collector_calendar_v1', {'days': ['2026-09-09', '2026-09-10']})
        with patch.object(market, 'cache', TTLCache()), patch('app.core.store.store', self.db), patch.object(tencent, 'kline_day_by_symbol', AsyncMock()) as t, patch.object(sina, 'kline_day', AsyncMock()) as s:
            days = await market.trade_days(10)
        t.assert_not_awaited(); s.assert_not_awaited()
        self.assertEqual(days, ['20260909', '20260910'])

class TopicRankReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'db.sqlite'))
        self.mode = settings.collector_mode
        settings.collector_mode = 'external'

    def tearDown(self):
        settings.collector_mode = self.mode
        self.tmp.cleanup()

    async def test_topic_rank_reads_exact_snapshot_without_fallback_or_compute(self):
        from app.services import topic as topic_service
        from app.main import app
        import httpx
        self.db.topic_snapshot_save('2026-09-10', [{'unique_key':'BK1','name':'绿色电力','score':94,'today_pct':1.2,'limit_up_count':6,'up_count':10,'stock_count':20,'leader_count':3,'reasons':['涨停']}])
        with patch('app.core.store.store', self.db), patch.object(topic_service, 'home_topics', AsyncMock()) as compute:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                ok = (await client.get('/v3/topic/tables/rank?date2=2026-09-10&limit=5&sort_by=rate')).json()
                missing = (await client.get('/v3/topic/tables/rank?date2=2026-09-09&limit=5')).json()
            compute.assert_not_awaited()
        self.assertEqual(ok['data']['status'], 'ok')
        self.assertEqual(ok['data']['items'][0]['topic_name'], '绿色电力')
        self.assertEqual(missing['data']['status'], 'missing')
        self.assertEqual(missing['data']['items'], [])

    async def test_topic_popular_reads_snapshot_and_hot_list(self):
        from app.services import popular as popular_svc, topic as topic_service
        from app.main import app
        import httpx
        self.db.topic_snapshot_save('2026-09-10', [{'unique_key':'BK1','name':'绿色电力','stocks':[{'code':'000001','name':'平安银行','pct':2,'lbc':1,'reason':'业绩'}]}])
        self.db.popular_save('2026-09-10', {'date':'2026-09-10','complete':True,'total':100,'items':[{'symbol_code': f'{i:06d}', 'symbol_name':'N'+str(i), 'rank': i, 'rank_diff': 0, 'last_pct': 1} for i in range(1,101)]})
        with patch('app.core.store.store', self.db), patch.object(topic_service, 'store', self.db), patch.object(popular_svc, 'store', self.db):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                body = (await client.get('/v3/topic/table/BK1/stocks/popular?date1=2026-09-10')).json()
                missing = (await client.get('/v3/topic/table/BK1/stocks/popular?date1=2026-09-09')).json()
        self.assertEqual(body['data']['status'], 'ok')
        self.assertEqual(body['data']['items'][0]['stock_code'], '000001')
        self.assertEqual(body['data']['items'][0]['rank'], 1)
        self.assertEqual(missing['data']['status'], 'missing')

class SentimentVipReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'db.sqlite'))
        self.mode = settings.collector_mode
        settings.collector_mode = 'external'

    def tearDown(self):
        settings.collector_mode = self.mode
        self.tmp.cleanup()

    async def test_timing_and_minute_distribution_use_published_samples(self):
        from app.services import intraday, market, index_history
        from app.datasources import tencent, eastmoney
        from app.core.cache import TTLCache
        from app.main import app
        import httpx
        self.db.kv_set('collector_calendar_v1', {'days': ['2026-09-09', '2026-09-10']})
        self.db.limit_pool_save('em_up','2026-09-10', {'pool_kind':'up','source':'eastmoney','date':'2026-09-10','requested_date':'2026-09-10','total':2,'complete':True,'empty_ok':True,'pool':[{'c':'000001','n':'平安银行','lbc':2},{'c':'000002','n':'万科A','lbc':1}]})
        self.db.limit_pool_save('em_down','2026-09-10', {'pool_kind':'down','source':'eastmoney','date':'2026-09-10','requested_date':'2026-09-10','total':0,'complete':True,'empty_ok':True,'pool':[]})
        self.db.minute_save('2026-09-10','09:31:00', {'date':'2026-09-10','minute':'09:31:00','up_num':3000,'down_num':1000,'avg_pct':1.2,'zb_num':2,'fb_num':2,'uplimit_num':2})
        self.db.minute_save('2026-09-10','15:00:00', {'date':'2026-09-10','minute':'15:00:00','up_num':2800,'down_num':1200,'avg_pct':0.8,'zb_num':3,'fb_num':2,'uplimit_num':2,'amount':1.5e11})
        bars = [{'trade_date':'2026-09-09','open':10,'close':11,'high':12,'low':9},{'trade_date':'2026-09-10','open':11,'close':12,'high':13,'low':10}]
        self.db.index_history_save('2026-09-10', {'date':'2026-09-10','complete':True,'total':1,'series':{'000002':{'code':'000002','symbol':'sh000002','name':'上证A指','bars':bars}},'coverage':['000002']})
        with patch.object(intraday, 'store', self.db), patch.object(pools, 'store', self.db), patch.object(market, 'cache', TTLCache()), patch.object(index_history, 'store', self.db), patch('app.core.store.store', self.db), patch.object(tencent, 'kline_day_by_symbol', AsyncMock()) as kline, patch.object(eastmoney, 'limit_up_pool', AsyncMock()) as up, patch.object(eastmoney, 'market_snapshot', AsyncMock()) as snap:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                timing = (await client.get('/v3/sentiment/timing?date1=2026-09-10')).json()['data']
                minute = (await client.get('/v3/sentiment/distribute_timing?date1=2026-09-10')).json()['data']
                missing = (await client.get('/v3/sentiment/distribute_timing?date1=2026-09-01')).json()['data']
                plate = (await client.get('/v3/market/kline/plate/883957?date1=2026-09-09')).json()['data']
                emotion = (await client.get('/v3/market/kline/plate/883404?date1=2026-09-10')).json()['data']
                kday = (await client.get('/v3/api/sentiment/kline/day/0?date1=2026-09-10')).json()['data']
        kline.assert_not_awaited(); up.assert_not_awaited(); snap.assert_not_awaited()
        self.assertTrue(timing)
        self.assertEqual(timing[-1]['date1'], '2026-09-10')
        self.assertIn('market_timing', timing[-1])
        self.assertEqual(len(minute), 2)
        self.assertEqual(minute[0]['minute'], '09:31:00')
        self.assertEqual(missing.get('status') or missing, 'missing' if isinstance(missing, dict) else missing)
        if isinstance(missing, dict):
            self.assertEqual(missing['status'], 'missing')
            self.assertEqual(missing['items'], [])
        self.assertEqual(plate[0]['b_name'], '同花顺全A')
        self.assertEqual(plate[-1]['date'], '2026-09-10')
        self.assertEqual(emotion[-1]['b_name'], '同花顺情绪')
        self.assertTrue(kday.get('items'))
        self.assertEqual(kday['items'][-1]['date'], '2026-09-10')
        self.assertIn('index', kday)


class ReasonAndPromotionReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'db.sqlite'))
        self.mode = settings.collector_mode
        settings.collector_mode = 'external'

    def tearDown(self):
        settings.collector_mode = self.mode
        self.tmp.cleanup()

    async def test_reason_page_uses_published_block_top_and_missing_is_empty(self):
        from app.services import board_hot, pools as pools_svc
        from app.datasources import ths, eastmoney
        from app.main import app
        import httpx
        self.db.limit_pool_save('em_up','2026-09-10', {'pool_kind':'up','source':'eastmoney','date':'2026-09-10','requested_date':'2026-09-10','total':1,'complete':True,'empty_ok':True,'pool':[{'c':'000001','n':'平安银行','lbc':2,'hybk':'银行','fbt':92500,'amount':100000000,'fund':123,'ltsz':100,'hs':2,'p':11000}], 'concepts_by_code':{'000001':['银行']}})
        self.db.limit_pool_save('ths_up','2026-09-10', {'pool_kind':'up','source':'10jqka','date':'2026-09-10','requested_date':'2026-09-10','total':1,'complete':True,'empty_ok':True,'pool':[{'code':'000001','reason':'短标签','high_days':'2天2板','open_num':0,'first_limit_up_time':'09:25'}]})
        self.db.block_top_save('2026-09-10', {'date':'2026-09-10','complete':True,'total':1,'items':[{'code':'TH001','name':'银行','limit_up_num':1,'stock_list':[{'code':'000001','name':'平安银行','reason_info':'长文原因','reason_type':'短标签','high':'2天2板'}]}], 'source':'10jqka_block_top'})
        self.db.kv_set('collector_calendar_v1', {'days':['2026-09-10','2026-09-11']})
        self.db.daily_close_save('2026-09-11', [{'stock_code':'000001','stock_name':'平安银行','open':12,'close':13,'prev_close':11,'high':13,'low':11}], {'complete':True,'trade_date':'2026-09-11'})
        with patch.object(board_hot, 'store', self.db), patch.object(pools_svc, 'store', self.db), patch('app.core.store.store', self.db), patch.object(ths, 'block_top', AsyncMock()) as fetch, patch.object(eastmoney, 'limit_up_pool', AsyncMock()) as up:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                rows = (await client.get('/v3/api/review/uplimit/reason?date1=2026-09-10&page=1&page_size=8')).json()['data']
                missing = (await client.get('/v3/api/review/uplimit/reason?date1=2026-09-01')).json()['data']
                hot = (await client.get('/v3/open/review/uplimit/board-hot?date1=2026-09-10')).json()['data']
        fetch.assert_not_awaited(); up.assert_not_awaited()
        self.assertEqual(rows[0]['plate_name'], '银行')
        self.assertEqual(rows[0]['stocks'][0]['reason'], '长文原因')
        self.assertEqual(missing, [])
        self.assertEqual(hot['plate_stocks'][hot['plate'][0][1]][0]['next_close_pct'], round((13/11-1)*100, 2))
        self.assertEqual(hot['plate_stocks'][hot['plate'][0][1]][0]['next_open_pct'], round((12/11-1)*100, 2))

    async def test_block_top_truncated_not_published(self):
        from app.services import pools as pools_svc
        from app.datasources import ths
        with patch.object(pools_svc, 'store', self.db), patch.object(ths, 'block_top', AsyncMock(side_effect=ValueError('Empty block_top'))):
            with self.assertRaises(ValueError):
                await pools_svc.collect_block_top('2026-09-10')
        self.assertIsNone(self.db.block_top_get('2026-09-10'))


class MarketHotDayReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(str(Path(self.tmp.name) / 'db.sqlite'))
        self.mode = settings.collector_mode
        settings.collector_mode = 'external'

    def tearDown(self):
        settings.collector_mode = self.mode
        self.tmp.cleanup()

    async def test_hot_day_uses_published_pools_without_upstream(self):
        from app.services import sentiment
        from app.datasources import eastmoney
        from app.core.cache import TTLCache
        from app.main import app
        import httpx
        self.db.kv_set('collector_calendar_v1', {'days': ['2026-09-09', '2026-09-10']})
        self.db.limit_pool_save('em_up','2026-09-10', {'pool_kind':'up','source':'eastmoney','date':'2026-09-10','requested_date':'2026-09-10','total':2,'complete':True,'empty_ok':True,'pool':[{'c':'000001','lbc':2,'zbc':1},{'c':'000002','lbc':1,'zbc':0}]})
        self.db.limit_pool_save('em_down','2026-09-10', {'pool_kind':'down','source':'eastmoney','date':'2026-09-10','requested_date':'2026-09-10','total':1,'complete':True,'empty_ok':True,'pool':[{'c':'000003'}]})
        with patch.object(sentiment, 'store', self.db), patch.object(pools, 'store', self.db), patch.object(sentiment, 'cache', TTLCache()), patch('app.core.store.store', self.db), patch.object(eastmoney, 'limit_up_pool', AsyncMock()) as up, patch('app.services.sentiment.trade_days', AsyncMock(return_value=['20260909','20260910'])):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                rows = (await client.get('/v3/api/sentiment/market/hot/day?date=2026-09-10')).json()['data']
                empty = (await client.get('/v3/api/sentiment/market/hot/day?date=2026-09-11')).json()['data']
        up.assert_not_awaited()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['date'], '2026-09-10')
        self.assertEqual(rows[0]['zt_num'], 2)
        self.assertEqual(rows[0]['dt_num'], 1)
        self.assertEqual(rows[0]['lb_high'], 2)
        self.assertEqual(empty, [])
