"""BaoStock历史字段规范化与缺口补录；不推导盘后时间、复权因子或题材成员。"""
from datetime import date, datetime
from zoneinfo import ZoneInfo
import math
import re
from app.core.store import store

SOURCE = 'baostock_daily_raw_v1'
FIELDS = 'date,code,open,high,low,close,preclose,volume,amount,adjustflag,tradestatus,pctChg,isST'


def number(value):
    if value in ('', None, '--'):
        return None
    n = float(value)
    if not math.isfinite(n):
        raise ValueError('非有限数值')
    return n


def normalize(rows, symbol, start, end):
    if date.fromisoformat(start).isoformat() != start or date.fromisoformat(end).isoformat() != end or start > end:
        raise ValueError('历史日期区间无效')
    if not re.fullmatch(r'(?:sh\.(?:60|68)\d{4}|sz\.(?:00|30)\d{4})', symbol):
        raise ValueError('本补采器仅接受沪深A股候选，拒绝指数/B股/北交所，防止六位代码串市场')
    dates, out = set(), []
    for raw in rows:
        d = date.fromisoformat(raw['date']).isoformat()
        if raw.get('code') != symbol or raw.get('adjustflag') != '3' or not start <= d <= end or d in dates:
            raise ValueError('行情代码、日期或不复权口径不一致')
        dates.add(d)
        values = {k: number(raw.get(k)) for k in ('open','high','low','close','volume','amount')}
        prices = [values[k] for k in ('open','high','low','close')]
        if any(v is not None and v < 0 for v in values.values()):
            raise ValueError('负行情字段')
        if all(v is not None for v in prices) and not 0 < values['low'] <= min(values['open'],values['close']) <= max(values['open'],values['close']) <= values['high']:
            raise ValueError('OHLC范围错误')
        provider_volume = values['volume']
        values['volume'] = provider_volume / 100 if provider_volume is not None else None
        state = raw.get('tradestatus')
        if state not in ('0','1','',None) or raw.get('isST') not in ('0','1','',None):
            raise ValueError('未知状态编码')
        if state == '0' and (values['volume'] not in (None,0) or values['amount'] not in (None,0)):
            raise ValueError('全天停牌声明与成交矛盾')
        # 保留供应商参考字段；其除权语义未核实时不填prev_close或tr_factor。
        meta = {'provider_preclose': number(raw.get('preclose')), 'provider_pct': number(raw.get('pctChg')),
                'provider_is_st': None if raw.get('isST') in ('',None) else raw['isST']=='1',
                'provider_trade_status': state, 'provider_volume_shares': provider_volume, 'volume_unit': 'lot_100_shares', 'amount_unit': 'CNY', 'history_source': SOURCE,
                'state_verified': state in ('0','1'), 'state_date': d,
                'state_source': SOURCE, 'security_state': {'0':'suspended','1':'trading'}.get(state)}
        out.append({'trade_date':d,'stock_code':symbol[3:],**values,'source':SOURCE,'input_meta':meta})
    return sorted(out,key=lambda r:r['trade_date'])


def apply_record(record):
    """原始响应不可覆盖；规范字段仅补空值，冲突不覆盖已有输入。"""
    if record.get('source') != SOURCE or record.get('complete_response') is not True:
        raise ValueError('缺少成功的独立来源响应')
    rows = normalize(record['rows'],record['symbol'],record['start'],record['end'])
    stamp = datetime.fromisoformat(record['collected_at'])
    if stamp.tzinfo is None or stamp > datetime.now(ZoneInfo('Asia/Shanghai')):
        raise ValueError('采集时点无效')
    if not rows or record['end'] >= stamp.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat():
        raise ValueError('历史补录仅接受采集日前的非空响应')
    key = store.kv_append_snapshot('ml_r1:baostock_raw:' + record['symbol'] + ':' + record['start'] + ':' + record['end'], record)
    for row in rows:
        row['input_meta']['history_evidence_key'] = key
        row['input_meta']['history_collected_at'] = record['collected_at']
    return {**store.kline_fill_missing(rows), 'raw_snapshot_id':key}
