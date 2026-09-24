import copy
import tempfile
import unittest
from unittest.mock import patch
from app.core.store import Store
from app.services import ml_r1 as m, ml_r1_service as svc
DAY='2026-09-14'
PRIOR=[f'2026-08-{i:02d}' for i in range(1,21)]
def stock(code):
    return dict(code=code, name=code, trade_date=DAY, close=100, prev_close=100,
                corporate_action={'verified':True,'kind':'none'}, listed_days=100, tradable=True, halted=False,
                amount=100, amount_history=[100]*20,amount_history_dates=PRIOR,
                limit_applicable=True,close_limit_up=False,close_limit_down=False,
                flow_definition='eastmoney_large_v1',flow_verified=True,flow_date=DAY,leader_buy=30,leader_sell=10)
def fixture():
    rows=[stock(str(i)) for i in range(5)]
    return dict(date=DAY,topics=[dict(code=f'BK{i:04d}',name=str(i),members=copy.deepcopy(rows),membership_verified=True,
                membership_known_at='2026-08-01T08:00:00+08:00',effective_from='2026-08-01') for i in range(20)],
                market=rows,market_full_universe=True,market_known_at='2026-08-01T08:00:00+08:00',
                source='local_independent',source_evidence='isolated fixture',prior_days=PRIOR,catalog_complete=True)
class FormulaTests(unittest.TestCase):
    def test_actions_compound(self):
        r={'close':90,'prev_close':100}
        self.assertIsNone(m.total_return(r));self.assertAlmostEqual(m.total_return(r,{'verified':True,'cash_dividend':10}),0)
        self.assertEqual(m.total_return({'close':50,'prev_close':100},{'verified':True,'split_factor':2}),0)
        self.assertIsNone(m.total_return(r,{'verified':True,'cash_dividend':1,'split_factor':2}))
        self.assertIsNone(m.total_return({**r,'total_return_factor':1.1}))
        self.assertAlmostEqual(m.compound([.1,-.1]),-.01);self.assertIsNone(m.compound([.1,None]));self.assertEqual(m.compound([-1,.2]),-1)
    def test_weights_mad_bins(self):
        self.assertAlmostEqual(sum(m.strength_contributions(dict(zip(m.WEIGHTS,[.6,.2,-.3,.4]))).values()),28)
        self.assertEqual(m.mad_z([3,3,3]),([0.,0.,0.],0.,True))
        self.assertEqual([m.pct_bucket(x) for x in [19.9,20,40,60,79.96,80,100,float('nan')]], [None,'20-40','40-60','60-80','60-80','80-100','100+',None])
    def test_full_group(self):
        r=m.compute_day(**fixture());self.assertEqual(r['status'],'final');self.assertEqual(r['rows'][0]['strength'],0)
        self.assertEqual(r['rows'][0]['coverage']['valid_price'],5);self.assertAlmostEqual(r['rows'][0]['money_strength'],.2)
    def test_membership_and_universe(self):
        f=fixture();f['topics'][0]['membership_known_at']='2026-09-14T10:00:00+08:00'
        self.assertIsNone(m.compute_day(**f)['rows'][0]['strength'])
        f=fixture();f['market_full_universe']=False;self.assertTrue(all(r['strength'] is None for r in m.compute_day(**f)['rows']))
    def test_unknown_inputs(self):
        for field in ['listed_days','close_limit_up','amount']:
            f=fixture();f['topics'][0]['members'][0][field]=None
            self.assertIsNone(m.compute_day(**f)['rows'][0]['strength'],field)
        f=fixture();f['topics'][0]['members'][0]['amount_history_dates'][-1]=DAY
        self.assertIsNone(m.compute_day(**f)['rows'][0]['strength'])
    def test_suspension_and_preview(self):
        f=fixture();r=stock('HALT');r['halted']=True;f['topics'][0]['members'].append(r)
        out=m.compute_day(**f);self.assertEqual(out['rows'][0]['coverage']['excluded'],1);self.assertEqual(out['rows'][0]['coverage']['price'],1)
        for topic in f['topics']:
            topic['members']=[stock(str(i)) for i in range(20)];topic['members'][0]['corporate_action']=None
        self.assertEqual(m.compute_day(**f)['status'],'partial_preview')
    def test_tiers_and_popularity(self):
        self.assertEqual(m.tiers([False,True,True])['consecutive'],2);self.assertIsNone(m.tiers([None,True])['consecutive'])
        self.assertEqual(m.tiers([True,True,False,True,False,True])['count'],4)
        self.assertIsNone(m.tiers([True,None,False,True,False,True])['count'])
        self.assertEqual(m.popularity_change(1,1),0);self.assertEqual(m.popularity_change(4,1),3);self.assertIsNone(m.popularity_change(None,1))
class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Store(self.tmp.name+'/test.db')
        self.p=patch.object(svc,'store',self.db);self.p.start()
        self.days=['2026-09-08','2026-09-09','2026-09-10','2026-09-11','2026-09-14']
        self.db.kv_set('collector_calendar_v1',{'complete':True,'days':self.days})
    def tearDown(self):self.p.stop();self.tmp.cleanup()
    def save(self,d,rate=.1,score=28,flow=10):
        row=dict(plate_code='BK0001',plate_name='test',return_value=rate,strength=score,leader_money=flow,
            metric_status={'strength':'final','return':'final','money':'final'},status='final',coverage={'price':1},
            flow_definition='eastmoney_large_v1',contributions=dict(zip(m.WEIGHTS,[24,6,-6,4])),
            member_returns={'TEST':{'return':rate,'stock_name':'test'}},membership_as_of='2026-01-01T00:00:00+08:00')
        row['return']=row.pop('return_value')
        self.db.ml_r1_snapshot_save(svc.TAXONOMY_VERSION,d,dict(date=d,source='local_independent',source_evidence='test',formula_version=m.VERSION,rows=[row],status='final'))
    def test_store_and_version(self):
        self.save(self.days[0]);self.assertIsNone(self.db.ml_r1_snapshot_get(svc.TAXONOMY_VERSION,self.days[0],plate_type=14))
        self.save(self.days[0],score=30)
        with self.db._conn() as c:self.assertEqual(c.execute('select count(*) from ml_r1_revisions').fetchone()[0],2)
        self.assertEqual(svc.get_day(17,self.days[0],svc.TAXONOMY_VERSION)['status'],'missing_input')
        self.assertEqual(svc.get_day(15,self.days[0],'wrong')['status'],'missing_input')
    def test_aggregation_and_gap(self):
        self.save(self.days[2],.1,20,10);self.save(self.days[4],-.1,40,20)
        self.assertIsInstance(svc.rank(15,self.days[4],3,9,12),dict)
        self.save(self.days[3],0,30,0)
        for order in [9,1,3]:
            r=svc.rank(15,self.days[4],3,order,12)[0]
            self.assertAlmostEqual(r['sum_rate'],-1);self.assertEqual(r['sum_score'],30);self.assertEqual(r['sum_leader_money'],30)
        self.assertIsInstance(svc.rank(15,'2026-09-13',1,9,12),dict)
    def test_source_and_null_money(self):
        self.save(self.days[4],flow=None);self.assertIsInstance(svc.rank(15,self.days[4],1,3,12),dict)
        r=self.db.ml_r1_snapshot_get(svc.TAXONOMY_VERSION,self.days[4]);r['source']='target_site';self.db.ml_r1_snapshot_save(svc.TAXONOMY_VERSION,self.days[4],r)
        self.assertEqual(svc.get_day(15,self.days[4])['reasons'],['source_not_allowlisted_or_unproven'])
    def test_pct_series(self):
        for d in self.days:self.save(d)
        r=svc.pct(15,'BK0001',[self.days[-1]],5)[self.days[-1]]
        self.assertEqual(r['meta']['status'],'final');self.assertAlmostEqual(r['stocks']['60-80'][0]['cum_pct'],61.051)
        self.assertEqual(svc.pct(15,'BK0001',[self.days[-1]],10)[self.days[-1]]['meta']['status'],'missing_input')
    def test_actual_ml_http_contract(self):
        import asyncio,httpx
        from app.main import app
        for d in self.days:self.save(d)
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
                params={'dates':','.join(self.days),'n_type':9,'n_days':1,'limit':12,'formula_id':'ml_r1','taxonomy_version':svc.TAXONOMY_VERSION}
                response=await client.get('/v3/market/hotspots/plates/15/rank/batch',params=params)
                self.assertEqual(response.status_code,200);cols=response.json()['data']['columns']
                self.assertEqual(len(cols),5);self.assertEqual(cols[0]['rows'][0]['sum_score'],28)
                params['formula_id']='unknown'
                self.assertEqual((await client.get('/v3/market/hotspots/plates/15/rank/batch',params=params)).status_code,422)
                params['formula_id']='ml_r1'
                response=await client.get('/v3/market/hotspots/plates/17/rank/batch',params=params)
                self.assertEqual(response.status_code,409)
                params['taxonomy_version']='target_ml_v1'
                response=await client.get('/v3/market/hotspots/plates/17/rank/batch',params=params)
                self.assertTrue(all(not c['rows'] for c in response.json()['data']['columns']))
        asyncio.run(run())

class PipelineTests(unittest.TestCase):
    def test_precompute_persists_inputs_and_blocks_missing_event_time(self):
        from app.services import ml_r1_pipeline as p
        with tempfile.TemporaryDirectory() as temp:
            db=Store(temp+'/test.db');db.kv_set('collector_calendar_v1',{'verified_at':'2026-09-14T21:00:00+08:00','days':PRIOR+[DAY]})
            with patch.object(p,'store',db),patch.object(svc,'store',db):
                f=fixture();f['taxonomy_version']=svc.TAXONOMY_VERSION
                result=p.precompute(f,15);self.assertEqual(result['status'],'missing_input')
                self.assertIn('missing_verified_eod_time',result['reasons'])
                f.update(session_kind='eod',source_as_of='2026-09-14T15:00:00+08:00')
                result=p.precompute(f,15);self.assertEqual(result['status'],'final')
                self.assertEqual(svc.rank(15,DAY,1,9,12)[0]['sum_score'],0)
                with db._conn() as c:self.assertEqual(c.execute('select count(*) from ml_r1_inputs').fetchone()[0],2)

class LimitTests(unittest.TestCase):
    def test_tick_rounding_and_broken_board(self):
        self.assertEqual(m.limit_price('10.05','.1'),11.06)
        self.assertEqual(m.limit_price('10.05','-.1'),9.05)
        self.assertEqual(m.limit_state(10.8,11,11,9)['broken_limit_up'],True)
        self.assertEqual(m.limit_state(11,11,11,9)['close_limit_up'],True)
        self.assertIsNone(m.limit_state(11,None,None,None)['close_limit_up'])
        self.assertIsNone(m.limit_price(10,None))

class PopularDisclosureTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_vendor_pool_and_unverified_rank_change(self):
        from app.services import free_hotspots as free
        with tempfile.TemporaryDirectory() as temp:
            db=Store(temp+'/test.db')
            db.kv_set('free_hotspots:members:BK1:index',[DAY]);db.kv_set('free_hotspots:members:BK1:'+DAY,{'as_of':DAY,'stocks':['000001'],'status':'ok','source':'eastmoney'})
            db.popular_save(DAY,{'date':DAY,'complete':True,'source':'10jqka_eq_hot_list','items':[{'symbol_code':'000001','rank':1,'rank_diff':14}],'total':1})
            with patch.object(free,'store',db):
                r=await free.popular_rank('BK1',DAY,1,30,with_pct=0)
                self.assertIsNone(r['list'][0]['rank_diff']);self.assertEqual(r['list'][0]['rank_diff_provider'],14)
                self.assertFalse(r['meta']['ranking_complete_for_board']);self.assertEqual(r['meta']['rank_pool_size'],1)

class PublicationRecoveryTests(unittest.TestCase):
    def test_failed_refresh_does_not_replace_valid_completed_day(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Store(temp+'/test.db')
            base={'date':DAY,'source':'local_independent','source_evidence':'test','formula_version':m.VERSION}
            db.ml_r1_snapshot_save(svc.TAXONOMY_VERSION,DAY,{**base,'status':'final','rows':[{'strength':28}]})
            db.ml_r1_snapshot_save(svc.TAXONOMY_VERSION,DAY,{**base,'status':'missing_input','rows':[],'reasons':['missing_market_universe']})
            r=db.ml_r1_snapshot_get(svc.TAXONOMY_VERSION,DAY)
            self.assertEqual(r['rows'][0]['strength'],28);self.assertEqual(r['latest_attempt']['status'],'missing_input')
