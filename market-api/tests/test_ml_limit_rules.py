import unittest
from app.services.ml_limit_rules import resolve
from app.services.ml_r1_pipeline import _limit_rule

class RulesTests(unittest.TestCase):
    def evidence(self,day,**extra):
        return {'date':day,'known_at':day+'T09:00:00+08:00','verified':True,'source':'isolated_exchange_reference','evidence_ref':'isolated','board':'sh_main','event':'ordinary','risk_warning':True,**extra}
    def test_st_effective_date_and_no_name_inference(self):
        for day,rate in [('2026-07-03',.05),('2026-07-06',.1)]:
            self.assertEqual(resolve(day,self.evidence(day))['rule']['rate'],rate)
        e=self.evidence('2026-07-03',risk_warning=None,stock_name='ST任何名称',code='600000')
        self.assertIsNone(resolve(e['date'],e)['rule'])
        e=self.evidence('2026-07-06',risk_warning=None);self.assertEqual(resolve(e['date'],e)['rule']['rate'],.1)
    def test_first_days_and_special_unknown(self):
        for board,first,last in [('sh_main',5,6),('chinext',5,6),('star',5,6),('bse',1,2)]:
            e=self.evidence('2026-09-14',board=board,event='ipo',event_trading_day=first)
            self.assertFalse(resolve(e['date'],e)['rule']['applicable'])
            e['event_trading_day']=last;self.assertTrue(resolve(e['date'],e)['rule']['applicable'])
        e=self.evidence('2026-09-14',board='star',event='delisting',event_trading_day=2)
        self.assertEqual(resolve(e['date'],e)['reasons'],['unverified_special_event_rule'])
    def test_pipeline_needs_dated_preopen_evidence(self):
        e=self.evidence('2026-09-14');self.assertEqual(_limit_rule({'security_rule_evidence':e},e['date'])['rate'],.1)
        e['known_at']='2026-09-14T15:00:00+08:00';self.assertIsNone(_limit_rule({'security_rule_evidence':e},e['date']))
        self.assertIsNone(resolve('2027-01-04',self.evidence('2027-01-04'))['rule'])
        self.assertIsNone(resolve('2026-09-14',None)['rule'])

    def test_limit_state_requires_actual_close_and_reference(self):
        from app.services.ml_r1_pipeline import _stock_row
        day='2026-09-14'
        meta={'security_rule_evidence':self.evidence(day),'state_verified':True,'state_date':day,'state_source':'isolated',
              'security_state':'trading','official_reference_verified':True,'eod_verified':True,'source_as_of':day+'T15:00:00+08:00'}
        bar={'close':11,'high':11,'prev_close':10,'input_meta':meta}
        def calculate():return _stock_row('600000','isolated','2000-01-01',day,[],[day],{'600000':{day:bar}}, {})
        self.assertTrue(calculate()['close_limit_up'])
        meta['source_as_of']=day+'T10:00:00+08:00';self.assertIsNone(calculate()['close_limit_up'])
        meta['source_as_of']=day+'T15:00:00+08:00';meta['official_reference_verified']=False
        self.assertIsNone(calculate()['limit_applicable'])
        meta['official_reference_verified']=True;meta['security_state']='suspended'
        self.assertIsNone(calculate()['close_limit_up'])

    def test_minimum_move_only_with_verified_rule(self):
        from app.services.ml_r1 import limit_price
        self.assertIsNone(limit_price('.04','.1'))
        self.assertEqual(limit_price('.04','.1',minimum_move=True),.05)
        self.assertEqual(limit_price('.04','-.1',minimum_move=True),.03)
        self.assertEqual(limit_price('.01','-.1',minimum_move=True),.01)
        self.assertTrue(resolve('2026-01-05',self.evidence('2026-01-05',board='sz_main'))['rule']['minimum_move'])


class EodRuleTests(unittest.IsolatedAsyncioTestCase):
    async def test_eod_preserves_security_evidence_for_calculation(self):
        import tempfile
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from unittest.mock import patch,AsyncMock
        from app.core.store import Store
        from app.services import ml_r1_kline as kline,ml_r1_pipeline as pipeline,ml_r1_service as service
        day='2026-09-14';now=datetime(2026,9,14,15,10,tzinfo=ZoneInfo('Asia/Shanghai'))
        evidence=RulesTests().evidence(day)
        snap={'trade_date':day,'complete':True,'source':'isolated','total':1,'stocks':[
            {'code':'600000','list_date':'20000101','source_timestamp':now.timestamp(),'price':11,'prev_close':10,'open':10,'high':11,'low':10,'amount':1100,'volume':1}]}
        with tempfile.TemporaryDirectory() as tmp:
            db=Store(tmp+'/db');db.kv_set('ml_r1:security_rule_evidence:600000:'+day,evidence)
            with patch.object(kline,'store',db),patch.object(kline.eastmoney,'market_snapshot',AsyncMock(return_value=snap)),patch.object(service,'calendar_days',return_value=['2026-09-11',day]),patch.object(kline,'datetime',wraps=datetime) as clock:
                clock.now.return_value=now
                await kline.update_eod(day)
            window=db.kline_window(day,day)
            self.assertEqual(window['600000'][day]['input_meta']['security_rule_evidence'],evidence)
            row=pipeline._stock_row('600000','isolated','2000-01-01',day,[],[day],window,{})
            self.assertTrue(row['close_limit_up'])
