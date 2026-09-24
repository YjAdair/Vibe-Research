import tempfile,unittest
from unittest.mock import patch
from app.core.store import Store
from app.services import baostock_history as b

class HistoryTests(unittest.TestCase):
    def row(self,**values):
        return dict(date='2026-01-05',code='sh.600000',open='10',high='11',low='9',close='10',volume='100',amount='1000',preclose='9.8',pctChg='2.0408',adjustflag='3',tradestatus='1',isST='0',**values)
    def test_validation_and_no_invented_evidence(self):
        r=b.normalize([self.row()],'sh.600000','2026-01-05','2026-01-06')[0]
        self.assertEqual(r['volume'],1);self.assertEqual(r['input_meta']['provider_volume_shares'],100)
        self.assertNotIn('prev_close',r);self.assertNotIn('tr_factor',r);self.assertNotIn('eod_verified',r['input_meta'])
        self.assertEqual(r['input_meta']['security_state'],'trading')
        raw=self.row();raw['amount']='';self.assertIsNone(b.normalize([raw],'sh.600000','2026-01-05','2026-01-06')[0]['amount'])
        for key,value in [('adjustflag','2'),('amount','NaN'),('high','8'),('tradestatus','0')]:
            raw=self.row();raw[key]=value
            with self.assertRaises(ValueError):b.normalize([raw],'sh.600000','2026-01-05','2026-01-06')
        for symbol in ['bj.920992','sh.000001','sz.200001']:
            with self.assertRaises(ValueError):b.normalize([],symbol,'2026-01-05','2026-01-06')
    def test_fill_preserves_existing_inputs_and_records_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Store(tmp+'/db');db.kline_daily_save([{'stock_code':'600000','trade_date':'2026-01-05','close':10,'amount':1000,'tr_factor':1.02,'source':'original','input_meta':{'eod_verified':True}}])
            record={'source':b.SOURCE,'symbol':'sh.600000','start':'2026-01-05','end':'2026-01-06','collected_at':'2026-09-15T09:00:00+08:00','rows':[self.row()],'complete_response':True}
            with patch.object(b,'store',db):out=b.apply_record(record)
            row=db.kline_window('2026-01-05','2026-01-05')['600000']['2026-01-05']
            self.assertEqual(row['amount'],1000);self.assertEqual(row['tr_factor'],1.02);self.assertTrue(row['input_meta']['eod_verified'])
            self.assertEqual(out['new_state_evidence'],1);self.assertEqual(row['source'],'original')
            record['rows'][0]['close']='9.5'
            with patch.object(b,'store',db):out=b.apply_record(record)
            self.assertEqual(out['conflicts'][0]['reason'],'price_conflict')
            self.assertEqual(db.kline_window('2026-01-05','2026-01-05')['600000']['2026-01-05']['close'],10)
    def test_explicit_suspension_keeps_null_amount(self):
        raw=self.row();raw.update(tradestatus='0',volume='0',amount='')
        row=b.normalize([raw],'sh.600000','2026-01-05','2026-01-06')[0]
        self.assertEqual(row['input_meta']['security_state'],'suspended');self.assertIsNone(row['amount'])

    def test_future_or_empty_archive_cannot_write(self):
        record={'source':b.SOURCE,'symbol':'sh.600000','start':'2026-01-05','end':'2026-01-06','collected_at':'2026-01-06T15:00:00+08:00','rows':[self.row()],'complete_response':True}
        with patch.object(b,'store') as db:
            with self.assertRaises(ValueError):b.apply_record(record)
            record.update(collected_at='2026-01-07T15:00:00+08:00',rows=[])
            with self.assertRaises(ValueError):b.apply_record(record)
            db.kv_append_snapshot.assert_not_called()

    def test_partial_merge_still_requires_valid_ohlc_and_state_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Store(tmp+'/db');db.kline_daily_save([{'stock_code':'600000','trade_date':'2026-01-05','close':10.01,'input_meta':{'state_verified':True,'state_date':'2026-01-05','security_state':'suspended'}}])
            raw=self.row();raw['high']='10'
            rows=b.normalize([raw],'sh.600000','2026-01-05','2026-01-06')
            self.assertEqual(db.kline_fill_missing(rows)['conflicts'][0]['reason'],'merged_ohlc_conflict')
            rows[0]['high']=11
            self.assertEqual(db.kline_fill_missing(rows)['new_state_evidence'],1)

    def test_three_failures_stop_before_sdk_or_network(self):
        import json,sys
        from pathlib import Path
        from scripts import backfill_baostock as cli
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);symbols=root/'symbols';symbols.write_text('sh.600000\n')
            db=Store(tmp+'/db');db.kv_set('ml_r1:baostock_login_failures',{'failures':3})
            argv=['backfill','--symbols-file',str(symbols),'--start','2026-01-05','--end','2026-01-06','--report',str(root/'report.json')]
            with patch.object(cli,'store',db),patch.object(sys,'argv',argv),patch.dict(sys.modules,{'baostock':None}),patch('builtins.print'):
                self.assertEqual(cli.main(),2)
            result=json.loads((root/'report.json').read_text())
            self.assertIn('login attempt limit reached',result['stop_reason'])
            self.assertEqual(db.kv_get('ml_r1:baostock_login_failures')['failures'],3)
