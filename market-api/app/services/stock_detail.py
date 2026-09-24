"""Explicit stock identity and date-bounded, unadjusted daily history."""
from datetime import datetime

from app.core.cache import cache
from app.datasources import tencent
from app.datasources.auction import TZ, number
from app.datasources.codes import to_tencent_symbol


async def detail(code: str, date: str | None, n: int = 120) -> dict:
    day = date or datetime.now(TZ).date().isoformat()
    symbol = to_tencent_symbol(code)  # 000001 here is SZ stock, never SH index.
    key = f'stock_detail_daily_v1:{symbol}'
    daily = cache.get(key)
    errors = []
    if daily is None:
        try:
            payload = await tencent.fetch_json(tencent.KLINE_URL, params={'param':f'{symbol},day,,,320,'})
            node = (payload.get('data') or {}).get(symbol) or {}
            if not node.get('day'):
                raise ValueError('Unadjusted daily series unavailable')
            rows = []
            for b in node['day']:
                if len(b) < 6:
                    raise ValueError('Truncated daily bar')
                date_value = datetime.strptime(b[0], '%Y-%m-%d').date().isoformat()
                values = [number(v) for v in b[1:6]]
                if any(v is None for v in values):
                    raise ValueError('Invalid daily number')
                o,c,h,l,v = values
                if min(o,c,h,l) <= 0 or v < 0 or h < max(o,c,l) or l > min(o,c,h):
                    raise ValueError('Invalid OHLC')
                rows.append({'date':date_value,'open':o,'close':c,'high':h,'low':l,'volume':v * 100})
            if len({r['date'] for r in rows}) != len(rows):
                raise ValueError('Duplicate daily date')
            daily = {'bars':sorted(rows,key=lambda r:r['date']), 'fetched_at':datetime.now(TZ).isoformat()}
            cache.set(key,daily,300)
        except Exception as exc:
            errors.append('daily:' + type(exc).__name__)
            daily = {'bars':[], 'fetched_at':None}
    bars = [r for r in daily['bars'] if r['date'] <= day][-n:]
    quote = None
    if day == datetime.now(TZ).date().isoformat():
        try:
            quotes = await tencent.realtime([code])
            item = quotes.get(code)
            if item:
                stamp = datetime.strptime(item['timestamp'],'%Y%m%d%H%M%S').replace(tzinfo=TZ)
                if stamp <= datetime.now(TZ):
                    quote = {**item,'source_as_of':stamp.isoformat(), 'is_selected_date':stamp.date().isoformat()==day,
                             'amount_yuan':item['amount']*10000}
        except Exception as exc:
            errors.append('quote:' + type(exc).__name__)
    return {'stock_code':code,'symbol':symbol,'date':day,'bars':bars,'quote':quote,
            'status':'partial' if errors else ('ok' if bars else 'missing'), 'errors':errors,
            'source':'tencent_public_quote','adjustment':'none','volume_unit':'shares',
            'daily_fetched_at':daily['fetched_at'], 'last_bar_date':bars[-1]['date'] if bars else None,
            'historical_limit':'供应商最近320根日线窗口；历史数据为当前取得的版本，未实现当时可见版本回放'}
