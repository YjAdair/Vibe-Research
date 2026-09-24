"""历史日线完成证据：独立原始价交叉核验，不伪造原始发布时间或回测可知时点。"""
import json
import math
from datetime import datetime, date
from app.core.store import store
from app.services import ml_r1

SOURCE = 'sina_daily_crosscheck_v1'
REFERENCE = 'https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData'


def complete_daily_bar(row, day):
    if not row or row.get('trade_date') != day:
        return False
    meta = row.get('input_meta') or {}
    if not isinstance(meta,dict):
        return False
    if meta.get('eod_verified') is True and ml_r1.verified_close_time(meta.get('source_as_of'), day):
        return True
    proof = meta.get('historical_daily') or {}
    if not isinstance(proof,dict):
        return False
    try:
        stamp = datetime.fromisoformat(proof['collected_at'])
        prices = [ml_r1.number(row.get(k)) for k in ('open','high','low','close')]
        return (proof.get('verified') is True and proof.get('date') == day
                and proof.get('source') == SOURCE and proof.get('reference') == REFERENCE
                and bool(proof.get('raw_snapshot_id')) and proof.get('crosschecked_source') == 'tencent_ifzq_ths_v1'
                and proof.get('price_basis') == 'unadjusted' and proof.get('ohlc') == prices
                and all(p is not None and p > 0 for p in prices)
                and stamp.tzinfo is not None and day < stamp.astimezone(ml_r1.TZ).date().isoformat()
                and stamp <= datetime.now(ml_r1.TZ))
    except (KeyError,TypeError,ValueError):
        return False


def verify_record(record, *, apply=False):
    """只为已存且与独立新浪原始日线一致的OHLC追加证据；不覆写任何价格。"""
    import re
    symbol = record.get('symbol','')
    if not re.fullmatch(r'(?:sh(?:60|68)\d{4}|sz(?:00|30)\d{4})',symbol):
        raise ValueError('仅接受沪深A股样板代码')
    if record.get('source') != SOURCE or record.get('reference') != REFERENCE or record.get('complete_response') is not True:
        raise ValueError('缺独立历史响应证据')
    stamp = datetime.fromisoformat(record['collected_at'])
    if stamp.tzinfo is None or stamp > datetime.now(ml_r1.TZ):
        raise ValueError('采集时间无效')
    start,end = record['start'],record['end']
    if date.fromisoformat(start).isoformat()!=start or date.fromisoformat(end).isoformat()!=end or not start<=end<stamp.astimezone(ml_r1.TZ).date().isoformat():
        raise ValueError('必须是采集日前的历史区间')
    rows = record.get('rows')
    if not isinstance(rows,list) or not rows:
        raise ValueError('空响应不能标记历史完成')
    seen,checked = set(),[]
    for raw in rows:
        day = date.fromisoformat(raw['day']).isoformat()
        if day in seen:
            raise ValueError('重复日线')
        seen.add(day)
        if not start<=day<=end:
            continue
        o,h,l,c = [ml_r1.number(raw.get(k)) for k in ('open','high','low','close')]
        volume = ml_r1.number(raw.get('volume'))
        if any(v is None for v in (o,h,l,c,volume)) or not 0<l<=min(o,c)<=max(o,c)<=h or volume<0:
            raise ValueError('历史OHLC或成交量无效')
        checked.append((day,{'open':o,'high':h,'low':l,'close':c},volume))
    if not checked:
        raise ValueError('请求区间没有日线')
    snapshot = store.kv_append_snapshot('ml_r1:historical_daily_raw:'+symbol,record) if apply else None
    report={'code':symbol[2:],'checked':len(checked),'verified':0,'written':0,'conflicts':[],'raw_snapshot_id':snapshot,'backtest_point_in_time_verified':False}
    with store._conn() as conn:
        if apply:conn.execute('BEGIN IMMEDIATE')
        for day,prices,volume in checked:
            old = conn.execute('SELECT * FROM stock_kline_daily WHERE stock_code=? AND trade_date=?',(symbol[2:],day)).fetchone()
            if not old or old['source']!='tencent_ifzq_ths_v1' or not all(ml_r1.number(old[k]) is not None and math.isclose(old[k],v,rel_tol=0,abs_tol=.005) for k,v in prices.items()):
                report['conflicts'].append({'date':day,'reason':'missing_independent_raw_bar_or_price_conflict'});continue
            report['verified']+=1
            if not apply:continue
            meta=json.loads(old['input_meta'] or '{}')
            meta['historical_daily']={'verified':True,'date':day,'source':SOURCE,'reference':REFERENCE,
                'collected_at':record['collected_at'],'known_at':record['collected_at'],'original_published_at':None,
                'raw_snapshot_id':snapshot,'crosschecked_source':old['source'],'price_basis':'unadjusted',
                'ohlc':[old[k] for k in ('open','high','low','close')]}
            # 历史日成交量大于零只能证明当日并非全天停牌，不构成盘前已知状态。
            if volume>0 and not meta.get('state_verified'):
                meta.update(state_verified=True,state_date=day,state_source=SOURCE,security_state='trading')
            conn.execute('UPDATE stock_kline_daily SET input_meta=? WHERE stock_code=? AND trade_date=?',
                         (json.dumps(meta,ensure_ascii=False),symbol[2:],day))
            report['written']+=1
    return report
