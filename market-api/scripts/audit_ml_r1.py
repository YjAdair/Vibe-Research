#!/usr/bin/env python3
"""逐分类/题材/交易日/指标审计，仅使用独立ML快照，输出CSV与JSON摘要。"""
import argparse,csv,json,sys,itertools
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.core.store import store
from app.services import ml_r1_service as svc, ml_r1_pipeline as pipe

def _ranges(missing, calendar):
    indexes = {d:i for i,d in enumerate(calendar)}
    groups = []
    for day in missing:
        if groups and indexes[day] == indexes[groups[-1][-1]] + 1:
            groups[-1].append(day)
        else:
            groups.append([day])
    return '|'.join(g[0] if len(g)==1 else g[0]+'..'+g[-1] for g in groups)


def audit_prices(days, out):
    """当前名录仅用于找采集缺口，绝不证明历史全市场成员有效。"""
    from app.services import ml_r1
    from app.services.ml_history import complete_daily_bar
    univ = None
    for key in sorted(store.kv_get('ml_r1:universe:index',[]) or [], reverse=True):
        record = store.kv_get(key)
        if record and record.get('stocks'):
            univ = record
            break
    candidates = {s['code']:s for s in (univ or {}).get('stocks', [])}
    fields = ('OHLC', 'amount', 'total_return_evidence', 'state_evidence', 'eod_evidence', 'historical_daily_evidence', 'limit_rule_evidence')
    totals, by_day, exchanges = Counter({f:0 for f in fields}), {d:Counter() for d in days}, {}
    stock_count = 0
    with (out/'ML-R1证券字段覆盖.csv').open('w',encoding='utf-8-sig',newline='') as file, store._conn() as conn:
        writer = csv.writer(file)
        writer.writerow(['代码','当前名录名称','当前名录上市日','历史名录已验证','候选日期数','有行日期数'] + [f for name in fields for f in (name+'有效数', name+'缺失交易日区间')])
        rows = conn.execute('SELECT stock_code,trade_date,open,high,low,close,amount,tr_factor,input_meta FROM stock_kline_daily WHERE trade_date>=? AND trade_date<=? ORDER BY stock_code,trade_date', (days[0], days[-1]))
        def emit(code, records):
            nonlocal stock_count
            stock_count += 1
            security = candidates.get(code,{})
            listing = pipe._iso_list_date(security.get('list_date'))
            expected = [d for d in days if not listing or d>=listing]
            seen = {r['trade_date']:dict(r) for r in records}
            valid = {f:[] for f in fields}
            exchange = 'BJ' if code.startswith(('43','83','87','92')) else 'HS'
            ex = exchanges.setdefault(exchange, Counter())
            ex['securities'] += 1
            if listing is None:
                ex['unknown_list_date'] += 1
            for d in expected:
                row = seen.get(d,{})
                try:
                    meta=json.loads(row.get('input_meta') or '{}')
                except (TypeError,ValueError):
                    meta={}
                state = meta.get('state_verified') is True and meta.get('state_date')==d and bool(meta.get('state_source'))
                checks = {'OHLC': all(ml_r1.number(row.get(f)) is not None and row[f]>0 for f in ('open','high','low','close')),
                          'amount': ml_r1.number(row.get('amount')) is not None and row['amount']>=0,
                          'total_return_evidence': meta.get('return_basis')=='total_return_v1_1' and ml_r1.number(row.get('tr_factor')) is not None,
                          'state_evidence': state,
                          'eod_evidence': meta.get('eod_verified') is True and ml_r1.verified_close_time(meta.get('source_as_of'),d),
                          'historical_daily_evidence': bool(meta.get('historical_daily')) and complete_daily_bar({**row,'input_meta':meta},d),
                          'limit_rule_evidence': bool(pipe._limit_rule(meta,d))}
                totals['candidate_security_dates'] += 1
                by_day[d]['candidate_security_dates'] += 1
                ex['candidate_security_dates'] += 1
                for field, ok in checks.items():
                    if ok:
                        valid[field].append(d)
                        totals[field] += 1
                        by_day[d][field] += 1
                        ex[field] += 1
            detail=[]
            for field in fields:
                known=set(valid[field])
                detail.extend([len(known),_ranges([d for d in expected if d not in known],days)])
            writer.writerow([code,security.get('name'),listing,False,len(expected),sum(d in seen for d in expected),*detail])
        seen_codes=set()
        for code,records in itertools.groupby(rows,lambda r:r['stock_code']):
            seen_codes.add(code)
            emit(code,records)
        for code in sorted(set(candidates)-seen_codes):
            emit(code,[])
    with (out/'ML-R1逐日字段覆盖.csv').open('w',encoding='utf-8-sig',newline='') as file:
        writer=csv.writer(file);writer.writerow(['日期','候选证券数',*fields,'历史名录已验证'])
        for d,counts in by_day.items():
            writer.writerow([d,counts['candidate_security_dates'],*[counts[f] for f in fields],False])
    return {'securities':stock_count,'window_from':days[0],'window_through':days[-1],
            'historical_universe_verified':False,'reference_universe_collected_at':(univ or {}).get('collected_at'),
            'note':'候选范围来自当前名录；含预热日期。原始字段存在不代表该日可发布评分；不得作为历史成分证据。',
            'counts':dict(totals),'exchanges':{k:dict(v) for k,v in exchanges.items()}}


def audit_auxiliary(days, out):
    """资金、人气和报告输入逐日核对；行数和时点有效数分开。"""
    from app.services import ml_r1
    from app.services.popular import INDEPENDENT_SOURCES
    flows = {}
    for row in store.stock_flow_range(days[0], days[-1]):
        flows.setdefault(row['trade_date'], []).append(row)
    totals = Counter()
    with (out/'ML-R1资金人气时点覆盖.csv').open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(['日期','资金原始行','净额定义时点有效数','资金来源','人气榜原始条目数','人气来源','独立榜单完整','人气行情时间','人气盘后时点有效','同时间排名变化基准已核验','盘后价格数','盘后未知证券数'])
        for day in days:
            rows = flows.get(day, [])
            run = store.kv_get('stock_flow_run:' + day, {}) or {}
            valid = sum(ml_r1.number(r.get('main_net_inflow')) is not None and ml_r1.verified_close_time(r.get('source_as_of'), day)
                        for r in rows) if run.get('source') == 'eastmoney_push2delay_f62' else 0
            popular = store.popular_get(day) or {}
            count = len(popular.get('items') or [])
            complete = popular.get('source') in INDEPENDENT_SOURCES and popular.get('complete') is True and popular.get('date') == day and popular.get('total') == count and count > 0
            timing = complete and ml_r1.verified_close_time(popular.get('source_as_of'), day)
            changes = complete and popular.get('rank_diff_basis') == 'previous_trading_day_same_time'
            eod = store.kv_get('ml_r1:eod_quote_run:' + day, {}) or {}
            writer.writerow([day, len(rows), valid, run.get('source'), count, popular.get('source'), complete, popular.get('source_as_of'), timing, changes, eod.get('priced_rows'), len(eod['missing_codes']) if isinstance(eod.get('missing_codes'), list) else None])
            totals['flow_raw_rows'] += len(rows)
            totals['flow_definition_time_verified_rows'] += valid
            totals['popular_independent_complete_days'] += int(complete)
            totals['popular_legacy_excluded_days'] += int(bool(popular) and popular.get('source') not in INDEPENDENT_SOURCES)
            totals['popular_eod_time_verified_days'] += int(timing)
            totals['popular_same_time_change_verified_days'] += int(changes)
    return dict(totals)


def audit(year,out):
    now=datetime.now(ZoneInfo('Asia/Shanghai'));today=now.date().isoformat()
    calendar=svc.calendar_days()
    completed=[d for d in calendar if d<today or (d==today and now.hour>=15)]
    if not completed:
        raise SystemExit('缺少可验证的交易日历，未生成年度覆盖结论')
    end=completed[-1]
    days=[d for d in calendar if f'{year}-01-01'<=d<=end]
    warmup=[d for d in calendar if d<f'{year}-01-01'][-20:]
    summary={'year':year,'from':days[0] if days else None,'through':end,'expected_trading_days':len(days),
             'warmup_days':warmup,'warmup_calendar_ready':len(warmup)==20,'formula_id':'ml_r1','formula_version':svc.VERSION,'coverage_verified':False,
             'target_catalog_complete':False,'metrics_implemented':True,'strategy_validated':False,'taxonomies':{},'as_of':today}
    out.mkdir(parents=True,exist_ok=True)
    with (out/'ML-R1年度逐项覆盖.csv').open('w',encoding='utf-8-sig',newline='') as file:
        w=csv.writer(file);w.writerow(['分类','代码','名称','日期','成员时点有效','强度','总收益','资金','价格覆盖','量能覆盖','涨停覆盖','缺口'])
        for typ in (14,15,17,18):
            catalog=pipe.native_catalog(typ) if typ in (14,15) else [{'plate_code':r['target_code'],'plate_name':r['target_name']} for r in store.ml_target_catalog() if r['taxonomy_id']==typ]
            snapshots={d:svc.get_day(typ,d) for d in days}
            maps={d:{r['plate_code']:r for r in s.get('rows',[])} for d,s in snapshots.items()}
            counts=Counter()
            for p in catalog:
                for d in days:
                    r=maps[d].get(p['plate_code'],{});metrics=r.get('metric_status',{});c=r.get('coverage',{})
                    statuses=[metrics.get(k,'missing_input') for k in ('strength','return','money')]
                    for k,v in zip(('strength','return','money'),statuses):counts[k+':'+v]+=1
                    reasons=r.get('reasons',snapshots[d].get('reasons',['missing_point_in_time_membership']))
                    w.writerow([typ,p['plate_code'],p['plate_name'],d,bool(r.get('membership_as_of')),*statuses,c.get('price'),c.get('volume'),c.get('limit'),svc.reason_text(reasons)])
            summary['taxonomies'][str(typ)]={'catalog_rows':len(catalog),'expected_board_dates':len(catalog)*len(days),
                 'catalog_historical_effective_dates_verified':False,'counts':dict(counts)}
    summary['raw_security_fields']=audit_prices(warmup+days,out)
    summary['auxiliary_inputs']=audit_auxiliary(days,out)
    summary['status']='unavailable' if not any(t['counts'].get('strength:final',0) for t in summary['taxonomies'].values()) else 'partial'
    (out/'ML-R1年度覆盖摘要.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    store.kv_set('ml_r1_annual_coverage',{str(year):summary})
    print(json.dumps(summary,ensure_ascii=False))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--year',type=int,default=datetime.now().year);p.add_argument('--out',default='../docs');args=p.parse_args();audit(args.year,Path(args.out))
