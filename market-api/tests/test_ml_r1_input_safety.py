"""隔离库回归：未知数据、快照不可变、多日正式值。"""
import tempfile, unittest
from unittest.mock import patch, AsyncMock
from datetime import datetime
from zoneinfo import ZoneInfo
from app.core.store import Store
from app.services import ml_r1 as m, ml_r1_service as svc, ml_r1_pipeline as pipe
from app.services import free_hotspots as free
from tests.test_ml_r1 import stock, fixture, DAY, PRIOR

class InputSafetyTests(unittest.TestCase):
    def row(self, hist):
        return pipe._stock_row('600000','s','2000-01-01',DAY,PRIOR,PRIOR+[DAY],{'600000':hist}, {})
    def test_invalid_snapshot_cannot_race_over_formal_snapshot(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as tmp:
            db=Store(tmp+'/race.db')
            base={'date':DAY,'source':'local_independent','source_evidence':'test','formula_version':m.VERSION}
            def save(i):
                db.ml_r1_snapshot_save(svc.TAXONOMY_VERSION,DAY,{**base,'status':'final' if i%2==0 else 'partial_preview','rows':[]})
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(save,range(16)))
            self.assertEqual(db.ml_r1_snapshot_get(svc.TAXONOMY_VERSION,DAY)['status'],'final')
    def test_unverified_flow_does_not_block_strength_or_become_money(self):
        f=fixture()
        for topic in f['topics']:
            for row in topic['members']:row['flow_verified']=False
        result=m.compute_day(**f)
        self.assertEqual(result['status'],'final')
        self.assertTrue(all(row['leader_money'] is None for row in result['rows']))

    def test_cash_action_passes_through_adapter_as_two_percent(self):
        from app.services.ml_r1_actions import return_evidence
        action=return_evidence(previous_close=10,close=10,official_reference=9.8,
                               previous_day="2026-09-11",day=DAY,
                               record={'verified':True,'complete':True,'events':[{'ex_date':DAY,'cash_ps':.2}]})
        cur={'close':10,'prev_close':9.8,'tr_factor':action['factor'],'action_day':True,
             'input_meta':{'return_basis':action['basis'],'eod_verified':True,'source_as_of':DAY+'T15:00:00+08:00',
                           'state_verified':True,'state_date':DAY,'state_source':'test','security_state':'trading'}}
        self.assertAlmostEqual(m.total_return(self.row({DAY:cur})),.02)

    def test_empty_provider_response_is_not_zero_pool(self):
        from app.datasources.eastmoney import _pool_payload
        for raw in ({'data':None},{'data':{'tc':0,'qdate':'20260910'}},
                    {'data':{'tc':0,'qdate':'20260909','pool':[]}}):
            with self.assertRaises(ValueError):
                _pool_payload(raw,'20260910')
        self.assertEqual(_pool_payload({'data':{'tc':0,'qdate':'20260910','pool':[]}},'20260910')['pool'],[])

    def test_missing_is_not_suspension_or_zero(self):
        r=self.row({}); self.assertIsNone(r['halted']);self.assertIsNone(r['tradable'])
        self.assertEqual(r['amount_history'],[None]*20)
        self.assertEqual(m._group([r],DAY,PRIOR,{})['coverage']['eligible'],1)
    def test_old_basis_and_intraday_are_not_verified(self):
        b={'close':10,'prev_close':10,'tr_factor':1,'amount':100,'input_meta':{'state_verified':True,'state_date':DAY,'security_state':'trading','state_source':'test','eod_verified':True,'source_as_of':DAY+'T10:00:00+08:00'}}
        r=self.row({DAY:b});self.assertFalse(r['total_return_verified']);self.assertIsNone(r['tradable']);self.assertIsNone(r['limit_applicable'])
    def test_only_explicit_suspension_can_zero_fill(self):
        b={'input_meta':{'security_state':'suspended','state_date':PRIOR[0],'state_verified':False}}
        self.assertIsNone(self.row({PRIOR[0]:b})['amount_history'][0])
        b['input_meta'].update(state_verified=True,state_source='exchange_notice')
        self.assertEqual(self.row({PRIOR[0]:b})['amount_history'][0],0)
    def test_net_flow_needs_no_invented_buy_sell(self):
        rows=[stock(str(i)) for i in range(5)]
        for r in rows:
            r.pop('leader_buy');r.pop('leader_sell');r.update(flow_definition='eastmoney_push2delay_f62',flow_verified=True,flow_date=DAY,main_net_inflow=-2)
        r=m._group(rows,DAY,PRIOR,{})
        self.assertEqual(r['leader_money'],-10);self.assertIsNone(r['money_leader_buy']);self.assertIsNone(r['money_leader_sell']);self.assertAlmostEqual(r['money_strength'],-.02)
    def test_partial_cross_section_never_becomes_final(self):
        f=fixture()
        for t in f['topics']:t['members']=[stock(str(i)) for i in range(20)]
        f['topics'][0]['members'][0]['corporate_action']=None
        self.assertTrue(all(r['metric_status']['strength']=='partial_preview' for r in m.compute_day(**f)['rows']))
    def test_multiday_rejects_partial(self):
        r={'plate_code':'BK1','plate_name':'t','strength':1,'return':.1,'metric_status':{'strength':'partial_preview'},'reasons':[]}
        with patch.object(svc,'get_day',return_value={'rows':[r]}):out=svc.rank(15,DAY,3,9,12,calendar=['2026-09-10','2026-09-11',DAY])
        self.assertIn('incomplete_window_final_values',out['reasons'])
    def test_retention_and_old_version_quarantine(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Store(tmp+'/test.db')
            with patch.object(pipe,'store',db),patch.object(svc,'store',db):
                for i in range(15):db.kv_append_snapshot('ml_r1:universe',{'collected_at':f'2026-09-14T08:{i:02}:00+08:00','complete':True,'stocks':[]})
                self.assertEqual(len(db.kv_get('ml_r1:universe:index')),15)
                self.assertEqual(pipe._latest_universe(DAY)['collected_at'],'2026-09-14T08:14:00+08:00')
                db.ml_r1_snapshot_save(svc.TAXONOMY_VERSION,DAY,{'date':DAY,'source':'local_independent','source_evidence':'test','formula_version':'ml_r1_v1','status':'final','rows':[]})
                self.assertEqual(svc.get_day(15,DAY)['reasons'],['formula_version_mismatch'])
class MemberRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_day_preopen_members_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Store(tmp+'/test.db');data={'total':1,'source':'eastmoney','stocks':[{'code':'600000'}]}
            with patch.object(free,'store',db),patch.object(free.boards,'members',AsyncMock(return_value=data)),patch.object(free,'datetime',wraps=datetime) as clock:
                clock.now.return_value=datetime(2026,9,14,9,tzinfo=ZoneInfo('Asia/Shanghai'));await free.collect_members('BK1')
                data['stocks']=[{'code':'600001'}];clock.now.return_value=datetime(2026,9,14,15,tzinfo=ZoneInfo('Asia/Shanghai'));await free.collect_members('BK1')
                keys=db.kv_get('free_hotspots:members:BK1:index');self.assertEqual(len(keys),2)
                records=[db.kv_get(k) for k in keys];before=[r for r in records if m.known_before(r['collected_at'],DAY+'T09:30:00')]
                self.assertEqual(before[0]['stocks'],['600000']);self.assertEqual(free.membership('BK1',DAY)['stocks'],['600001'])
    async def test_kline_backend_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Store(tmp+'/test.db');db.board_history_save(3,'BK1',[{'date1':d,'plate_code':'BK1','open':10,'high':12,'low':9,'close':11} for d in ['2026-09-11',DAY]])
            with patch.object(free,'store',db):r=await free.kline('BK1',end='2026-09-11')
            self.assertEqual(r['x'],['20260911']);self.assertEqual(r['series_kind'],'ohlc')
if __name__=='__main__':unittest.main()
