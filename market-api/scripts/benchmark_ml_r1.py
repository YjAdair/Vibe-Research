"""非空ML-R1路由性能，临时SQLite合成输入；绝不写生产库。"""
import asyncio,copy,json,math,sys,tempfile,time,logging
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from app.core.store import Store
from app.services import ml_r1 as m,ml_r1_service as svc
from app.main import app
from tests.test_ml_r1 import fixture

async def main():
    dates=svc.calendar_days()[-10:]
    with tempfile.TemporaryDirectory() as temp:
        db=Store(temp+'/bench.db');db.kv_set('collector_calendar_v1',{'complete':True,'days':dates})
        for date in dates:
            f=fixture();f['date']=date;template=f['topics'][0]
            f['topics']=[{**copy.deepcopy(template),'code':f'BK{i:04d}','name':f'合成{i}'} for i in range(500)]
            for r in f['market']+[r for t in f['topics'] for r in t['members']]:r['trade_date']=date
            result=m.compute_day(**f);assert result['status']=='final'
            db.ml_r1_snapshot_save(svc.TAXONOMY_VERSION,date,result)
        latencies=[]
        with patch.object(svc,'store',db):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://isolated') as client:
                for i in range(22):
                    t=time.perf_counter();response=await client.get('/v3/market/hotspots/plates/15/rank/batch',params={'dates':','.join(dates),'n_days':1,'n_type':9,'limit':12,'formula_id':'ml_r1','taxonomy_version':svc.TAXONOMY_VERSION})
                    elapsed=(time.perf_counter()-t)*1000;body=response.json();assert response.status_code==200
                    assert sum(len(c['rows']) for c in body['data']['columns'])==120
                    if i>=2:latencies.append(elapsed)
        result={'scope':'临时SQLite + ASGI真实路由，500合成题材/日，非生产行情；不含网络传输','formula_id':'ml_r1','rows':120,'samples':20,'p95_ms':sorted(latencies)[18],'latencies_ms':latencies}
        dest=Path(__file__).resolve().parents[2]/'docs/ML-R1非空性能验证.json';dest.write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in result.items() if k!='latencies_ms'},ensure_ascii=False))
if __name__=='__main__':
    logging.getLogger('httpx').setLevel(logging.ERROR);asyncio.run(main())
