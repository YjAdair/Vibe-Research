#!/usr/bin/env python3
"""有截止时间的独立历史补洞。可选baostock SDK仅在此CLI载入；--apply前需备份。"""
import argparse,json,signal,socket,sys,time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.core.store import store
from app.services.baostock_history import FIELDS,SOURCE,normalize,apply_record


def timed_out(*args):
    raise TimeoutError('source request timed out')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbols-file',type=Path,required=True)
    parser.add_argument('--start',required=True);parser.add_argument('--end',required=True)
    parser.add_argument('--seconds',type=int,default=600);parser.add_argument('--limit',type=int,default=100)
    parser.add_argument('--apply',action='store_true');parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    start=datetime.strptime(args.start,'%Y-%m-%d').date();end=datetime.strptime(args.end,'%Y-%m-%d').date()
    if start>end or end>=datetime.now(ZoneInfo('Asia/Shanghai')).date():parser.error('仅补截至昨日的历史日线，不声明当日收盘已经更新')
    if not 1<=args.seconds<=5400 or not 1<=args.limit<=6000:parser.error('单轮最多5400秒/6000证券，仍须受任务总截止约束')
    symbols=list(dict.fromkeys(s.strip() for s in args.symbols_file.read_text().splitlines() if s.strip()))[:args.limit]
    # 验证代码在任何网络请求之前完成；北交所接口已明确不支持，不重试。
    for symbol in symbols:normalize([],symbol,args.start,args.end)
    deadline=time.monotonic()+args.seconds
    socket.setdefaulttimeout(8);signal.signal(signal.SIGALRM,timed_out)
    out={'started_at':datetime.now().astimezone().isoformat(),'source':SOURCE,'apply':args.apply,'start':args.start,'end':args.end,'total_candidates':len(symbols),'completed':0,'skipped':0,'failed':0,'inserted_rows':0,'filled_fields':0,'new_state_evidence':0,'conflicts':0,'results':[],'coverage_verified':False}
    def save():
        out['updated_at']=datetime.now().astimezone().isoformat();args.report.parent.mkdir(parents=True,exist_ok=True)
        tmp=args.report.with_suffix('.tmp');tmp.write_text(json.dumps(out,ensure_ascii=False,indent=2));tmp.replace(args.report)
    consecutive_errors=0
    login_key='ml_r1:baostock_login_failures'
    login_state=store.kv_get(login_key,{}) or {}
    logged_in=False
    login_attempted=False
    bs=None
    try:
        if login_state.get('failures',0)>=3:
            raise RuntimeError('login attempt limit reached')
        import baostock as bs
        login_attempted=True
        signal.alarm(min(20,args.seconds));lg=bs.login();signal.alarm(0)
        if lg.error_code!='0':raise RuntimeError('BaoStock login '+lg.error_code)
        logged_in=True
        for symbol in symbols:
            if time.monotonic()>=deadline or consecutive_errors>=3:
                out['stop_reason']='deadline' if time.monotonic()>=deadline else 'three_consecutive_source_errors';break
            unit='ml_r1:baostock_job:'+symbol+':'+args.start+':'+args.end
            previous=store.kv_get(unit,{}) or {}
            if previous.get('applied') or previous.get('failures',0)>=3:
                out['skipped']+=1;continue
            try:
                signal.alarm(max(1,min(30,int(deadline-time.monotonic()))))
                rs=bs.query_history_k_data_plus(symbol,FIELDS,start_date=args.start,end_date=args.end,frequency='d',adjustflag='3')
                raw=[]
                while rs.error_code=='0' and rs.next():raw.append(dict(zip(rs.fields,rs.get_row_data())))
                if rs.error_code!='0':raise RuntimeError(rs.error_code+':'+rs.error_msg)
                if not raw:raise ValueError('empty historical response')
                rows=normalize(raw,symbol,args.start,args.end)
                signal.alarm(0)
                record={'source':SOURCE,'symbol':symbol,'start':args.start,'end':args.end,'rows':raw,'collected_at':datetime.now().astimezone().isoformat(),'complete_response':True,'source_reference':'https://pypi.org/project/baostock/','sdk_version':'0.9.3'}
                result=apply_record(record) if args.apply else {'validated_rows':len(rows)}
                result['conflict_count']=len(result.get('conflicts',[]))
                result.update(symbol=symbol,rows=len(rows),status='applied' if args.apply else 'validated')
                if args.apply:store.kv_set(unit,{**previous,'applied':True,'rows':len(rows),'result':result})
                out['completed']+=1;consecutive_errors=0
                for field in ('inserted_rows','filled_fields','new_state_evidence'):out[field]+=result.get(field,0)
                out['conflicts']+=result.get('conflict_count',0)
            except Exception as exc:
                signal.alarm(0);consecutive_errors+=1;out['failed']+=1
                result={'symbol':symbol,'status':'failed','error':type(exc).__name__+':'+str(exc)}
                store.kv_set(unit,{**previous,'failures':previous.get('failures',0)+1,'error':result['error']})
            out['results'].append(result);save()
            if out['completed']%50==0 or result['status']=='failed':print(json.dumps({k:v for k,v in out.items() if k!='results'},ensure_ascii=False),flush=True)
        out.setdefault('stop_reason','candidate_batch_finished')
    except Exception as exc:
        out['stop_reason']=type(exc).__name__+':'+str(exc)
        if login_attempted and not logged_in and login_state.get('failures',0)<3:
            store.kv_set(login_key,{'failures':login_state.get('failures',0)+1,'error':out['stop_reason'],'at':datetime.now().astimezone().isoformat()})
    finally:
        signal.alarm(0)
        # 直接关闭公开匿名会话，避免退出阶段再触发无界网络等待。
        if bs is not None:
            import baostock.common.context as context
            sock=getattr(context,'default_socket',None)
            if sock:sock.close()
        save();print(json.dumps({k:v for k,v in out.items() if k!='results'},ensure_ascii=False),flush=True)

    return 2 if out.get('failed') or out.get('stop_reason') not in ('candidate_batch_finished','deadline') else 0


if __name__=='__main__':sys.exit(main())
