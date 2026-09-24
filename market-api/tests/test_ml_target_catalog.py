"""隔离数据库中的目标分类接线验收；合成数据不计入生产覆盖率。"""
import asyncio
import copy
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch
import httpx
from app.core.store import Store
from app.services import ml_catalog as cat, ml_r1_service as svc, ml_r1_pipeline as pipeline, free_hotspots
from tests.test_ml_r1 import fixture, DAY, PRIOR


def catalog():
    return dict(taxonomy_version=cat.VERSION, trade_date=DAY,
                known_at=DAY+'T09:00:00+08:00', collected_at=DAY+'T16:00:00+08:00',
                source='local_independent', source_evidence='isolated test fixture, not production',
                definition_status='verified', definition_ref='isolated taxonomy definition', reviewed_by='test',
                complete_levels=[17], nodes=[dict(plate_type=17, code=f'80{i:04d}', name=f'test{i}',
                    membership_status='verified', members=[f'{j:06d}' for j in range(1,6)], expected_members=5,
                    source='local_independent', membership_evidence_ref='isolated dated members') for i in range(20)] +
                    [dict(plate_type=18, code='809999', name='child', parent_code='800000', membership_status='verified',
                          members=['000001','000002'], expected_members=2, source='local_independent', membership_evidence_ref='isolated child')])


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.db=Store(self.temp.name+'/db.sqlite'); self.stack=ExitStack()
        for module in (cat,svc,pipeline,free_hotspots): self.stack.enter_context(patch.object(module,'store',self.db))
        self.db.kv_set('collector_calendar_v1',dict(complete=True,days=PRIOR+[DAY]))
    def tearDown(self): self.stack.close(); self.temp.cleanup()
    def prepare(self):
        record=catalog(); key=cat.ingest(record,apply=True)['catalog_snapshot_id']; payload=fixture()
        for i,row in enumerate(payload['market']): row.update(code=f'{i+1:06d}',name=f'{i+1:06d}')
        for i,topic in enumerate(payload['topics']):
            topic['code']=f'80{i:04d}'; topic['members']=copy.deepcopy(payload['market'])
        payload.update(taxonomy_version=cat.VERSION,catalog_snapshot_id=key,session_kind='eod',source_as_of=DAY+'T15:00:00+08:00')
        return payload,pipeline.precompute(payload,17)
    def test_validation_and_exact_day(self):
        for field,value in [('known_at',DAY+'T10:00:00+08:00'),('source','target_site'),('source_evidence','https://quant.zizizaizai.com/members')]:
            record=catalog();record[field]=value
            with self.assertRaises(ValueError):cat.ingest(record)
        record=catalog();record['nodes'][0]['members'][0]=100001
        with self.assertRaises(ValueError):cat.ingest(record)
        record=catalog();record['nodes'][-1]['parent_code']='800999'
        with self.assertRaises(ValueError):cat.ingest(record)
        key=cat.ingest(catalog(),apply=True)['catalog_snapshot_id']
        self.assertIsNone(cat.get('2026-09-11'));self.assertIsNone(cat.get(DAY,'arbitrary-key'))
        self.assertEqual(cat.get(DAY)['catalog_snapshot_id'],key)
    def test_compute_and_frozen_membership(self):
        payload,out=self.prepare();self.assertEqual(out['status'],'final')
        self.assertIsNone(out['collected_at']);self.assertIsNotNone(out['computed_at'])
        self.assertEqual(len(svc.rank(17,DAY,1,9,12)),12)
        self.assertEqual(svc.rank(17,DAY,1,9,12)[0]['taxonomy_version'],cat.VERSION)
        self.assertEqual(svc.daily_nav(17,'800000',DAY,1)['series_kind'],'daily_nav')
        revised=catalog();revised['collected_at']=DAY+'T17:00:00+08:00';revised['nodes'][0]['members']=['000006'];revised['nodes'][0]['expected_members']=1
        revised['nodes'][-1]['members']=['000003'];revised['nodes'][-1]['expected_members']=1
        cat.ingest(revised,apply=True)
        self.assertEqual(cat.prepared_membership(DAY,18,'809999')['stocks'],['000001','000002'])
        self.assertEqual(cat.prepared_membership(DAY,17,'800000')['stocks'],['000001','000002','000003','000004','000005'])
        self.assertEqual(cat.subplates(17,'800000',[DAY])['stocks'][DAY]['809999'],['000001','000002'])
        bad=copy.deepcopy(payload);bad['topics'][0]['members'].pop()
        with self.assertRaises(ValueError):pipeline.precompute(bad,17)
        bad=copy.deepcopy(payload);bad['topics'].append(bad['topics'][0])
        with self.assertRaises(ValueError):pipeline.precompute(bad,17)
    def test_missing_catalog_never_publishes(self):
        payload,_=self.prepare();payload.pop('catalog_snapshot_id');pipeline.precompute(payload,18)
        self.assertIsInstance(svc.rank(18,DAY,1,9,12),dict)
        self.assertEqual(cat.prepared_membership(DAY,18,'809999')['status'],'target_catalog_snapshot_mismatch')

    def test_partial_disclosure_members_can_be_read_but_never_claim_complete(self):
        record=catalog();record['complete_levels']=[]
        record['nodes'][0].update(membership_status='partial',expected_members=None)
        cat.ingest(record,apply=True)
        member=cat.prepared_membership(DAY,17,'800000')
        self.assertEqual(member['status'],'partial_membership');self.assertFalse(member['membership_complete'])
        self.assertEqual(len(member['stocks']),5)
        payload=pipeline.build_local_input(DAY,17)
        self.assertEqual(len(payload['topics'][0]['members']),5)
        self.assertFalse(payload['topics'][0]['membership_verified'])
        result=pipeline.precompute(payload,17)
        self.assertNotEqual(result['status'],'final')
        from app.main import app
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                response=await client.get('/v3/market/hotspots/plates/17/800000/stocks/rates',params={'date1':DAY,'formula_id':'ml_r1'})
                data=response.json()['data'];self.assertEqual(data['meta']['status'],'partial_membership')
                self.assertEqual(len(data['list']),5);self.assertIsNone(data['meta']['expected_members'])
        asyncio.run(run())

    def test_retrospective_sample_keeps_real_known_time_and_rejects_future_news(self):
        record=catalog();record.update(complete_levels=[],definition_status='sample',knowledge_basis='retrospective_disclosure_sample',
                                      known_at=DAY+'T17:00:00+08:00',collected_at=DAY+'T17:00:00+08:00')
        for node in record['nodes']:
            node.update(membership_status='partial',expected_members=None,
                        member_evidence={c:{'reference':'https://independent.example/disclosure','published_at':'2025-12-01T00:00:00+08:00'} for c in node['members']})
        key=cat.ingest(record,apply=True)['catalog_snapshot_id']
        self.assertEqual(cat.get(DAY,key)['known_at'],DAY+'T17:00:00+08:00')
        bad=copy.deepcopy(record);bad['nodes'][0]['member_evidence']['000001']['published_at']='2026-09-15T00:00:00+08:00'
        with self.assertRaises(ValueError):cat.ingest(bad)
        bad=copy.deepcopy(record);bad['complete_levels']=[17]
        with self.assertRaises(ValueError):cat.ingest(bad)
        formal_key=cat.ingest(catalog(),apply=True)['catalog_snapshot_id']
        self.assertEqual(cat.get(DAY)['catalog_snapshot_id'],formal_key)
    def test_adapter_quote_return_is_not_investment_return(self):
        from tests.test_ml_r1_input_safety import InputSafetyTests
        from app.services import ml_r1
        cur={'close':10,'prev_close':9.8,'tr_factor':1.02,'input_meta':{
            'return_basis':'total_return_v1_1','eod_verified':True,'source_as_of':DAY+'T15:00:00+08:00',
            'official_reference_verified':True,'state_verified':True,'state_date':DAY,'state_source':'isolated', 'security_state':'trading'}}
        row=InputSafetyTests().row({DAY:cur})
        self.assertAlmostEqual(row['quote_return'],10/9.8-1)
        self.assertAlmostEqual(ml_r1.total_return(row),.02)
        cur['input_meta']['official_reference_verified']=False
        self.assertIsNone(InputSafetyTests().row({DAY:cur})['quote_return'])

    def test_backend_routes(self):
        self.prepare()
        from app.main import app
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
                paths=[f'/plates/17/rank/batch?dates={DAY}&n_type=9',f'/plates/17/trend?plate_code=800000&day_start={DAY}&day_end={DAY}',
                       f'/plates/17/800000/sub-plates-stocks?dates={DAY}',f'/plates/18/809999/stocks/rates?date1={DAY}',
                       f'/plates/18/809999/stocks/rank?date1={DAY}',f'/plates/17/800000/stocks/pct/batch?dates={DAY}&days=5&sub_plate_code=809999',
                       f'/kline/800000?plate_type=17&date2={DAY}&n=1',f'/steps?board=800000&date1={DAY}&plate_type=17&sub_plate_code=809999']
                for path in paths:
                    r=await client.get('/v3/market/hotspots'+path+'&formula_id=ml_r1')
                    self.assertEqual(r.status_code,200,(path,r.text));self.assertNotIn('unsupported_taxonomy',r.text)
                r=await client.get(f'/v3/market/hotspots/steps?board=800001&date1={DAY}&plate_type=17&sub_plate_code=809999&formula_id=ml_r1')
                self.assertEqual(r.status_code,422)
                r=await client.get(f'/v3/market/hotspots/plates/17/800001/stocks/pct/batch?dates={DAY}&days=5&sub_plate_code=809999&formula_id=ml_r1')
                self.assertEqual(r.status_code,422)
                r=await client.get(f'/v3/market/hotspots/plates/18/809999/stocks/rates?date1={DAY}&formula_id=ml_r1&taxonomy_version=eastmoney_native_v1')
                self.assertEqual(r.status_code,409)
        asyncio.run(run())
