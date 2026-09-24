import json,tempfile,unittest
from unittest.mock import patch
from app.core.store import Store
from app.services import ml_history as history,free_hotspots,ml_r1_pipeline


class HistoricalDailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Store(self.tmp.name+'/db')
        self.db.kline_daily_save([{'stock_code':'000063','trade_date':'2026-01-05','open':10,'high':11,'low':9,'close':10.5,'amount':1000,'source':'tencent_ifzq_ths_v1'}])
        self.record={'source':history.SOURCE,'reference':history.REFERENCE,'symbol':'sz000063','start':'2026-01-05','end':'2026-01-05',
                     'complete_response':True,'collected_at':'2026-09-15T09:00:00+08:00',
                     'rows':[{'day':'2026-01-05','open':'10','high':'11','low':'9','close':'10.5','volume':'100'}]}
    def tearDown(self):self.tmp.cleanup()
    def apply(self):
        with patch.object(history,'store',self.db):return history.verify_record(self.record,apply=True)
    def test_historical_display_does_not_invent_point_in_time_or_return(self):
        self.assertEqual(self.apply()['written'],1)
        bar=self.db.kline_by_codes('2026-01-05',['000063'])['000063']
        self.assertTrue(history.complete_daily_bar(bar,'2026-01-05'))
        self.assertNotIn('eod_verified',bar['input_meta']);self.assertIsNone(bar['tr_factor'])
        with patch.object(free_hotspots,'store',self.db):rows=free_hotspots._quote_rows(['000063'],'2026-01-05',{'quotes':{'000063':{'name':'中兴通讯'}}})
        self.assertEqual(rows[0]['stock_name'],'中兴通讯')
        self.assertEqual(rows[0]['last_px'],10.5);self.assertEqual(rows[0]['amount'],1000)
        self.assertEqual(rows[0]['data_status'],'historical_close');self.assertIsNone(rows[0]['source_as_of'])
        self.assertIsNone(rows[0]['px_change_rate']);self.assertFalse(rows[0]['price_evidence']['backtest_point_in_time_verified'])
        adapted=ml_r1_pipeline._stock_row('000063','s','2000-01-01','2026-01-05',[],['2026-01-05'],{'000063':{'2026-01-05':bar}}, {})
        self.assertIsNone(adapted['tradable'])  # 历史展示证据不自动升级为当时盘后评分输入。
        bar['close']=10.6;self.assertFalse(history.complete_daily_bar(bar,'2026-01-05'))
    def test_bad_or_intraday_response_cannot_be_final_history(self):
        for field,value in [('end','2026-09-15'),('symbol','sh000001'),('complete_response',False)]:
            before=self.record[field];self.record[field]=value
            with self.assertRaises(ValueError):self.apply()
            self.record[field]=before
        self.record['rows'][0]['close']='NaN'
        with self.assertRaises(ValueError):self.apply()
    def test_disagreement_quarantines_without_replacing_price(self):
        self.record['rows'][0]['close']='10.6';out=self.apply()
        self.assertEqual(out['written'],0);self.assertEqual(len(out['conflicts']),1)
        row=self.db.kline_by_codes('2026-01-05',['000063'])['000063']
        self.assertEqual(row['close'],10.5);self.assertNotIn('historical_daily',row['input_meta'])
