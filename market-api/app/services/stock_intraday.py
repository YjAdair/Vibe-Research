"""Demand-collected intraday sessions; exact-date readback and source isolation."""
import asyncio
from datetime import datetime
from app.core.cache import cache
from app.core.store import store
from app.datasources import tencent
from app.datasources.auction import TZ
from app.datasources.codes import to_tencent_symbol
from app.datasources.intraday import parse_session

_locks = {}


def now_local():
    return datetime.now(TZ)


async def collect(symbol: str) -> dict:
    key = f'stock_intraday_v1:{symbol}'
    async with _locks.setdefault(symbol,asyncio.Lock()):
        hit = cache.get(key)
        if hit:
            return hit
        report = {'available_dates':[], 'errors':[], 'fetched_at':now_local().isoformat()}
        sessions = []
        try:
            data = await tencent.fetch_json(tencent.DAY_QUERY_URL,params={'code':symbol})
            node = (data.get('data') or {}).get(symbol) or {}
            if not node.get('data'):
                raise ValueError('No sessions for requested stock')
            seen = set()
            for raw in node['data']:
                try:
                    session = parse_session(raw['date'],raw['data'],raw.get('prec'))
                    stamp = datetime.fromisoformat(session['source_as_of'])
                    if stamp > now_local() or session['date'] in seen:
                        raise ValueError('Future or duplicate session')
                    seen.add(session['date'])
                    session.update(symbol=symbol,source='tencent_day_query',fetched_at=report['fetched_at'])
                    sessions.append(session)
                    report['available_dates'].append(session['date'])
                except Exception as exc:
                    report['errors'].append('session:' + type(exc).__name__)
            store.stock_intraday_save(symbol,sessions)
        except Exception as exc:
            report['errors'].append('source:' + type(exc).__name__)
        report['available_dates'] = sorted(report['available_dates'],reverse=True)
        cache.set(key,report,10 if report['errors'] else 30)
        return report


async def review(code: str, date: str | None) -> dict:
    day = date or now_local().date().isoformat()
    symbol = to_tencent_symbol(code)
    stored = store.stock_intraday_get(symbol,day)
    # A complete historical snapshot is usable without a live provider request.
    historical = day < now_local().date().isoformat()
    if historical and stored and stored['points'][-1]['time'] == '15:00' and stored.get('missing_times') == []:
        report = {'available_dates':store.stock_intraday_dates(symbol), 'errors':[], 'fetched_at':stored['fetched_at']}
    else:
        report = await collect(symbol)
        stored = store.stock_intraday_get(symbol,day)
    result = {k:v for k,v in (stored or {}).items() if k not in ('raw_rows','excluded_rows')}
    return {**result,'stock_code':code,'symbol':symbol,'date':day,'points':result.get('points',[]),
            'available_dates':report['available_dates'],'refresh_errors':report['errors'],
            'status':'refresh_failed' if report['errors'] else ('partial' if result.get('missing_times') else 'ok' if stored else 'missing'),
            'served_from_history':historical and bool(stored),
            'limitations':['仅展示09:30–11:30、13:00–15:00的供应商记录，盘后数据单独保留但不混入图表',
                           '成交量/额柱为相邻可用记录累计值之差；首个记录包含开盘竞价，缺分钟不填造',
                           '均价按累计成交额/累计股数计算，受供应商成交量舍入影响',
                           '历史为采集时取得的版本；不是当时可见版本。未采集且超出供应商窗口的日期为空']}
