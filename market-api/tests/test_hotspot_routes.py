import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.api import hotspot_routes
from app.config import settings
from app.core.cache import TTLCache
from app.core.collector import TZ, due_jobs
from app.core.store import Store
from app.datasources import eastmoney
from app.main import app
from app.services import hotspot_history


class FreeHotspotIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_formula_version_is_rejected_before_read(self):
        import httpx
        from app.main import app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            response=await client.get('/v3/market/hotspots/capabilities',params={'formula_version':'ml_r1_v1'})
            self.assertEqual(response.status_code,409)

    async def test_rank_routes_to_free_service_and_rejects_reference_code(self):
        fetch = AsyncMock(return_value=[{'plate_code': 'BK0475', 'sum_rate': 1.25, 'sum_score': None}])
        with patch.object(hotspot_routes.service, 'rank_days', fetch):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.get('/v3/market/hotspots/plates/15/rank/days?date2=20260911')
                bad = await client.get('/v3/market/hotspots/kline/801660')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['meta']['origin_data_used'])
        self.assertEqual(response.json()['data'][0]['plate_code'], 'BK0475')
        self.assertEqual(bad.status_code, 200)
        self.assertEqual(bad.json()['meta']['status'], 'missing')
        self.assertEqual(bad.json()['data']['series_kind'], 'daily_nav')
        self.assertEqual(bad.json()['data']['y'], [])

    async def test_capabilities_expose_unverified_target_taxonomy_and_observed_catalog(self):
        import httpx
        from app.main import app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/v3/market/hotspots/capabilities')
        body = response.json()['data']
        self.assertEqual(body['taxonomies']['17']['status'], 'missing_input')
        self.assertTrue(body['taxonomies']['17']['implemented'])
        self.assertEqual(body['taxonomies']['18']['status'], 'missing_input')
        self.assertEqual({(row['taxonomy_id'], row['target_code']) for row in body['target_catalog']},
                         {(17, '801660'), (18, '801003')})
        self.assertTrue(all(row['verification_status'] == 'unverified' for row in body['vendor_mappings']))

    async def test_kline_does_not_sync_backfill_when_embedded(self):
        import httpx
        from app.main import app
        from app.config import settings
        from app.services import hotspot_history
        with patch.object(settings, 'collector_mode', 'embedded'), \
             patch.object(hotspot_history, 'collect', AsyncMock(side_effect=AssertionError('must not backfill'))):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.get('/v3/market/hotspots/kline/BK999999?plate_type=14')
        self.assertEqual(response.status_code, 200)

    async def test_legacy_target_paths_are_parameterized_and_never_return_bk_rows(self):
        import httpx
        from app.main import app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            urls = [
                '/v3/market/hotspots/plates/17/trend?plate_code=BK0475&day_start=20260910&day_end=20260911',
                '/v3/market/hotspots/plates/17/BK0475/stocks/pct/batch?dates=20260911',
                '/v3/market/hotspots/plate/popular/reason?plate_code=BK0475',
                '/v3/market/hotspots/steps?board=BK0475&date1=20260911',
            ]
            responses = [await client.get(url) for url in urls]
        self.assertEqual([r.status_code for r in responses], [200, 200, 422, 422])
        self.assertTrue(all(response.json()['meta']['status'] == 'unsupported_taxonomy' for response in responses[:2]))

    async def test_native_parameters_reject_target_codes_and_keep_native_taxonomy(self):
        import httpx
        from app.main import app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            rejected = await client.get('/v3/market/hotspots/plates/14/801660/stocks/rates?date1=20260911')
            rejected2 = await client.get('/v3/market/hotspots/plates/15/801003/stocks/rank?date1=20260911')
            with patch.object(hotspot_routes.service, 'trend', AsyncMock(return_value=[])):
                industry = await client.get('/v3/market/hotspots/plates/14/trend?plate_code=BK0475&day_start=20260910&day_end=20260911')
            with patch.object(hotspot_routes.service, 'stock_rates', AsyncMock(return_value={'list': []})):
                concept = await client.get('/v3/market/hotspots/plates/15/BK0475/stocks/rates?date1=20260911')
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(rejected2.status_code, 422)
        self.assertEqual(industry.json()['meta']['taxonomy'], 'eastmoney_industry')
        self.assertEqual(concept.json()['meta']['taxonomy'], 'eastmoney_concept')

    async def test_unsupported_response_does_not_inherit_eastmoney_source(self):
        import httpx
        from app.main import app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/v3/market/hotspots/plates/17/rank/days?date2=20260911')
        self.assertIsNone(response.json()['meta']['source'])
        self.assertIsNone(response.json()['data']['source'])

    async def test_capabilities_do_not_claim_full_year_coverage(self):
        import httpx
        from app.main import app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            body = (await client.get('/v3/market/hotspots/capabilities')).json()['data']
        self.assertFalse(body['coverage_verified'])
        self.assertFalse(body['taxonomies']['14']['coverage_verified'])
        self.assertFalse(body['taxonomies']['15']['coverage_verified'])
        coverage = body['annual_coverage']['2026']
        self.assertIn(coverage['status'], ('unavailable', 'not_audited'))
        self.assertFalse(coverage['coverage_verified'])
        self.assertTrue(body['formulas']['ml_r1']['implemented'])
        self.assertFalse(body['formulas']['ml_r1']['data_ready'])

    async def test_target_catalog_parent_relation_is_persisted(self):
        import httpx
        from app.main import app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            rows = (await client.get('/v3/market/hotspots/capabilities')).json()['data']['target_catalog']
        by_code = {row['target_code']: row for row in rows}
        self.assertIsNone(by_code['801660']['parent_taxonomy_id'])
        self.assertIsNone(by_code['801660']['parent_code'])
        self.assertEqual(by_code['801003']['parent_taxonomy_id'], 17)
        self.assertEqual(by_code['801003']['parent_code'], '801660')

    async def test_rank_batch_limits_dates_validates_dates_and_isolates_targets(self):
        import httpx
        from app.main import app
        dates = ','.join(['20260911'] * 121)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            too_many = await client.get(f'/v3/market/hotspots/plates/15/rank/batch?dates={dates}')
            invalid = await client.get('/v3/market/hotspots/plates/15/rank/batch?dates=20260911,bad-date')
            with patch.object(hotspot_routes.service, 'rank_days', AsyncMock(side_effect=AssertionError('target must not read'))):
                target = await client.get('/v3/market/hotspots/plates/17/rank/batch?dates=20260911,20260910')
        self.assertEqual(too_many.status_code, 422)
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(target.status_code, 200)
        self.assertEqual(target.json()['meta']['status'], 'unsupported_taxonomy')
        self.assertIsNone(target.json()['meta']['source'])
        self.assertEqual([row['status'] for row in target.json()['data']['columns']],
                         ['unsupported_taxonomy', 'unsupported_taxonomy'])

    async def test_rank_batch_returns_dated_column_status_and_native_taxonomy(self):
        import httpx
        from app.main import app
        with patch.object(hotspot_routes.service, 'rank_days', AsyncMock(return_value=[])) as rank_days:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.get('/v3/market/hotspots/plates/15/rank/batch?dates=20260911,20260910&n_days=1&n_type=1&limit=12')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['meta']['taxonomy'], 'eastmoney_concept')
        self.assertEqual(body['data']['meta']['taxonomy'], 'eastmoney_concept')
        self.assertEqual([row['status'] for row in body['data']['columns']], ['missing', 'missing'])
        self.assertEqual(rank_days.await_count, 2)

    async def test_tier_uses_consecutive_limit_count_not_interval_count(self):
        snap = {'pool': [{'c': '000001', 'n': '样本', 'lbc': 2, 'zttj': {'ct': 4}, 'fbt': 93001}]}
        with patch.object(hotspot_routes.service, 'membership', return_value={'stocks': ['000001']}), \
                patch.object(hotspot_routes.pools, 'published', side_effect=[snap, {'pool': []}]):
            body = await hotspot_routes.steps('BK0475', '2026-09-11', plate_type=15)
        row = body['data']['plate_stocks']['BK0475'][0]
        self.assertEqual(row['up_limit_keep_times'], 2)
        self.assertEqual(row['up_limit_time'], '09:30:01')
        self.assertIsNone(row['fd_max'])

    def test_default_schedule_excludes_origin_jobs(self):
        with patch.object(settings, 'enable_origin_reference', False):
            jobs = dict(due_jobs(datetime(2026, 9, 14, 20, 35, tzinfo=TZ)))
        self.assertFalse({'plate_rank', 'plate_reason', 'plate_members'} & set(jobs))
        self.assertIn('kaipanla_plate_finalize', jobs)
        self.assertIn('kaipanla_plate_history', jobs)

    async def test_eastmoney_unknown_quote_does_not_become_zero(self):
        payload = {'data': {'total': 1, 'diff': [{'f12': '000001', 'f2': '-', 'f3': '-', 'f18': 10.0}]}}
        with patch.object(eastmoney, 'fetch_json', AsyncMock(return_value=payload)):
            result = await eastmoney.board_stocks('BK0475')
        self.assertIsNone(result['stocks'][0]['price'])
        self.assertIsNone(result['stocks'][0]['pct'])
        self.assertEqual(result['stocks'][0]['prev_close'], 10)

    async def test_history_free_success_persists_native_series_without_paid(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Store(str(Path(tmp) / 'test.db'))
            rows = [{'date1': '2026-09-11', 'open': 10, 'price': 11, 'high': 12,
                     'low': 9, 'amount': 100, 'volume': 10, 'pct': 10}]
            fetch = AsyncMock(return_value=rows)
            from app.services import data_fallback
            with patch.object(hotspot_history, 'store', db), patch.object(hotspot_history, 'cache', TTLCache()), \
                    patch.object(hotspot_history.board_history, 'fetch_series', fetch), \
                    patch.object(hotspot_history.QVerisGateway, 'binding', side_effect=hotspot_history.QVerisUnavailable()), \
                    patch.object(hotspot_history.QVerisGateway, 'execute', AsyncMock()) as paid, \
                    patch.object(data_fallback, '_get_store', return_value=db):
                report = await hotspot_history.collect('BK0475', 3, '样本', '2026-09-11')
                await hotspot_history.collect('BK0475', 3, '样本', '2026-09-11')
            self.assertEqual(report['source'], 'free')
            self.assertEqual(fetch.await_count, 1)
            paid.assert_not_awaited()
            saved = db.board_history_range(3, '2026-09-11', '2026-09-11')
            self.assertEqual(saved[0]['plate_code'], 'BK0475')
            self.assertEqual(saved[0]['source'], 'eastmoney')


    async def test_manual_stop_survives_process_cache_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Store(str(Path(tmp) / 'test.db'))
            key = 'free_hotspot_history:BK1023:2026-03-16:2026-09-11'
            db.kv_set(key, {'status':'unavailable', 'manual_probe_stopped':True})
            with patch.object(hotspot_history, 'store', db), patch.object(hotspot_history, 'cache', TTLCache()), \
                    patch.object(hotspot_history.board_history, 'fetch_series', AsyncMock()) as fetch:
                report = await hotspot_history.collect('BK1023', 3, '培育钻石', '2026-09-11')
            self.assertTrue(report['manual_probe_stopped'])
            fetch.assert_not_awaited()
