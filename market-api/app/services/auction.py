"""Auction read model, timestamp-validated collection and explicit signal coverage."""
from __future__ import annotations

import asyncio
from datetime import datetime, time

from app.core.cache import cache
from app.core.store import store
from app.datasources import eastmoney, tencent
from app.datasources.auction import TZ, extract, limit_buy_from_quote, number
from app.services import market
from app.config import settings

_lock = asyncio.Lock()


def now_local():
    return datetime.now(TZ)


def day_iso(value: str) -> str:
    return datetime.strptime(value.replace('-', ''), '%Y%m%d').date().isoformat()


async def sessions(day: str) -> list[str]:
    days = sorted({day_iso(d) for d in await market.trade_days(250) if day_iso(d) <= day})
    # The cached daily series can lag today's session. Confirm via exchange-dated
    # index quote; never manufacture a calendar from Monday-Friday.
    if day == now_local().date().isoformat() and day not in days:
        quotes = await tencent.realtime_by_symbol('sh000001')
        q = quotes.get('000001', {})
        if str(q.get('timestamp', '')).startswith(day.replace('-', '')):
            days.append(day)
    return days


async def candidates(day: str, previous: str) -> dict:
    key = f'auction_candidates_v1:{day}'
    saved = store.kv_get(key)
    if saved:
        return saved
    up, failed = await asyncio.gather(eastmoney.limit_up_pool(previous.replace('-', '')), eastmoney.limit_broken_pool(previous.replace('-', '')))
    stocks = []
    seen = set()
    daily = []
    for pool, broken in ((up, False), (failed, True)):
        if len(pool['pool']) != int(pool['total']):
            raise ValueError('Incomplete candidate pool')
        # qdate is the vendor's latest available date, NOT the returned pool date.
        # Multi-date probes verified requested historical pools differ.
        if not pool.get('qdate') or day_iso(str(pool['qdate'])) < previous:
            raise ValueError('Candidate provider has not published requested session')
        for s in pool['pool']:
            code = str(s.get('c', ''))
            if len(code) != 6 or not code.isdigit() or code in seen:
                raise ValueError('Invalid/duplicate candidate')
            seen.add(code)
            industry = s.get('hybk') or ''
            stocks.append({'stock_code': code, 'stock_name': s.get('n', ''), 'failed': broken,
                           'level': 0 if broken else int(s.get('lbc') or 1),
                           'plates': [{'plate_code': 'industry:' + industry, 'plate_name': industry, 'score': None}] if industry else []})
            amount = number(s.get('amount'))
            if amount is not None and amount >= 0:
                daily.append({'date': previous, 'stock_code': code, 'daily_trade_amount': amount,
                              'field_sources': {'daily_trade_amount': {'source': 'eastmoney_historical_limit_pool', 'requested_date': previous}}})
    if not stocks:
        raise ValueError('Candidate pools empty; no complete candidate snapshot available')
    data = {'date': day, 'candidate_date': previous, 'stocks': stocks, 'collected_at': now_local().isoformat(),
            'basis': 'previous_session_limit_up_and_broken', 'raw_pools': {'up': up, 'failed': failed}}
    store.auction_save(daily, [])
    store.kv_set(key, data)
    return data


async def collect(day: str, days: list[str]) -> dict:
    async with _lock:
        key = f'auction_collect_v1:{day}'
        hit = cache.get(key)
        if hit:
            return hit
        now = now_local()
        if day != now.date().isoformat() or day not in days or len(days) < 2:
            raise ValueError('Auction collector only accepts the current confirmed session')
        universe = await candidates(day, days[-2])
        codes = [s['stock_code'] for s in universe['stocks']]
        sem = asyncio.Semaphore(3)
        async def batch(part):
            async with sem:
                return await tencent.realtime(part)
        results = await asyncio.gather(*(batch(codes[i:i + 80]) for i in range(0, len(codes), 80)), return_exceptions=True)
        rows, evidence, errors = [], [], []
        for result in results:
            if isinstance(result, Exception):
                errors.append(type(result).__name__)
                continue
            for code, q in result.items():
                if code not in codes:
                    continue
                at = now_local()
                row = extract(q, day, at)
                if row:
                    rows.append(row)
                    evidence.append({'date': day, 'collected_at': at.isoformat(), 'quote': q})
        store.auction_save(rows, evidence)
        report = {'status': 'collected' if len(rows) == len(codes) else 'partial', 'expected': len(codes),
                  'received': len(rows), 'auction_amount_count': sum(r['auction_amount'] is not None for r in rows),
                  'errors': errors, 'collected_at': now_local().isoformat()}
        store.kv_set(f'auction_report_v1:{day}', report)
        cache.set(key, report, 10 if time(9, 25) <= now.time() < time(9, 30) else 300)
        return report


def signals(rows: list[dict]) -> list[dict]:
    """Newest first. Unknown inputs produce unknown rules, never false signals."""
    def val(i, k):
        return number(rows[i].get(k)) if i < len(rows) else None
    def rule(values, formula):
        return None if any(v is None for v in values) else bool(formula(*values))
    def rise(a, b):
        return a > 0 if b == 0 else (a - b) / abs(b) > .5
    out = []
    for i, row in enumerate(rows):
        one = [val(j, k) for k in ('up_limit_buy_amount', 'auction_pct', 'auction_amount') for j in (i, i+1)]
        r1 = rule(one, lambda a,b,c,d,e,f: rise(a,b) and rise(c,d) and rise(e,f))
        r2 = rule([val(i+1,'daily_trade_amount'), val(i+2,'daily_trade_amount'),val(i,'auction_amount'),val(i,'auction_pct')],
                  lambda p,pp,a,pct: p > pp and p > 5e8 and a/p > .1 and pct > 5)
        # OR is evaluable with one known true branch even if the other is missing.
        buy = rule([val(i,'up_limit_buy_amount'),val(i+1,'up_limit_buy_amount')],lambda a,b:a>b)
        amount = rule([val(i,'auction_amount'),val(i+1,'auction_amount')],lambda a,b:a>b)
        pct = rule([val(i,'auction_pct'),val(i+1,'auction_pct')],lambda a,b:a>b)
        either = True if buy is True or amount is True else (False if buy is False and amount is False else None)
        r3 = False if pct is False or either is False else (True if pct is True and either is True else None)
        rules = {'1':r1,'2':r2,'3':r3}
        vol = rule([val(i,'daily_trade_amount'),val(i+1,'daily_trade_amount')],lambda a,b:a>5e8 and rise(a,b))
        out.append({**row, 'signal': ','.join(k for k,v in rules.items() if v is True) or 0,
                    'signal_rules': rules, 'signal_complete': all(v is not None for v in rules.values()), 'volume_increase': vol})
    return out


def backfill_limit_buy(day: str) -> dict:
    """Derive up_limit_buy_amount from stored in-window auction evidence.

    Real-time extraction drops quotes whose provider timestamp lags more than
    60 seconds, but the evidence row is still stored. This rebuilds the field
    from the last in-window quote per stock and never overwrites a known value.
    """
    latest = {}
    for item in store.auction_evidence_range(day, day):
        quote = item.get('quote') or {}
        stamp = str(quote.get('timestamp', ''))
        if len(stamp) < 14 or not ('092500' <= stamp[8:14] < '093000'):
            continue
        if quote.get('code') not in latest or stamp > latest[quote.get('code')][0]:
            latest[quote['code']] = (stamp, item.get('collected_at'), quote)
    rows = []
    for code, (stamp, collected, quote) in latest.items():
        value = limit_buy_from_quote(quote)
        if value is None or value <= 0:
            continue
        source_as_of = datetime.strptime(stamp, '%Y%m%d%H%M%S').replace(tzinfo=TZ).isoformat()
        rows.append({'date': day, 'stock_code': code, 'up_limit_buy_amount': value,
                     'source': 'tencent_public_quote', 'source_as_of': source_as_of,
                     'collected_at': collected or source_as_of,
                     'field_sources': {'up_limit_buy_amount': {
                         'source': 'tencent_public_quote', 'source_as_of': source_as_of,
                         'basis': 'auction price at daily up-limit; bid1 unmatched hands * 100 * up-limit price; rebuilt from stored evidence'}}})
    saved = 0
    if rows:
        known = {r['stock_code'] for r in store.auction_range(day, day) if r.get('up_limit_buy_amount') is not None}
        rows = [r for r in rows if r['stock_code'] not in known]
        if rows:
            store.auction_save(rows, [])
            saved = len(rows)
    return {'date': day, 'evidence_codes': len(latest), 'derived': len(rows), 'saved': saved}


async def review(date: str | None = None, count: int = 3) -> dict:
    day = day_iso(date) if date else now_local().date().isoformat()
    published = store.kv_get('collector_calendar_v1',{}) if settings.collector_mode != 'embedded' else {}
    days = [d for d in published['days'] if d <= day] if published.get('days') else await sessions(day)
    refresh_error = None
    if not date and days:
        day = days[-1]
    if settings.collector_mode == 'embedded' and day == now_local().date().isoformat() and day in days:
        try:
            await collect(day, days)
        except Exception as exc:
            refresh_error = type(exc).__name__
    selected = [d for d in days if d <= day][-count:][::-1]
    universe = store.kv_get(f'auction_candidates_v1:{day}') or {}
    data = store.auction_range(selected[-1], day) if selected else []
    by_key = {(r['stock_code'], r['date']): r for r in data}
    ladder = {}
    themes = {}
    for s in universe.get('stocks', []):
        rows = signals([by_key.get((s['stock_code'], d), {'date':d}) for d in selected])
        item = {**s, 'auction':rows}
        ladder.setdefault(s['level'], []).append(item)
        for p in s['plates']:
            themes.setdefault(p['plate_code'], {**p, 'count':0})['count'] += 1
    candidate_codes = {s['stock_code'] for s in universe.get('stocks', [])}
    current_rows = [r for r in data if r['date'] == day and r['stock_code'] in candidate_codes]
    stamps = [r['source_as_of'] for r in current_rows if r.get('source_as_of')]
    keys = ['auction_pct','auction_amount','auction_turnover','up_limit_buy_amount','auction_net_amount']
    coverage = {k: sum(r.get(k) is not None for r in current_rows) for k in keys}
    return {'date':day, 'trade_days':selected, 'candidate_date':universe.get('candidate_date'),
            'ladder':[{'level':level,'stocks':ladder[level]} for level in sorted(ladder, reverse=True)],
            'themes':sorted(themes.values(), key=lambda p:(-p['count'],p['plate_name'])),
            'status':'refresh_failed' if refresh_error else ('partial' if universe else 'missing'),
            'refresh_error':refresh_error, 'report':store.kv_get(f'auction_report_v1:{day}'),
            'refresh_mode':settings.collector_mode,
            'coverage':coverage,'total':len(universe.get('stocks', [])),
            'source_time_range': {'min':min(stamps), 'max':max(stamps)} if stamps else None,
            'candidate_basis':'前一交易日涨停及炸板股；原站候选范围尚未验证',
            'classification_basis':'东财行业；原站题材分类和分数尚未对齐',
            'signal_version':'public_frontend_v1_missing_aware',
            'limitations':['涨幅由当日开盘价与昨收价计算；仅有开盘价不能证明集合竞价阶段发生了成交',
                           '竞价成交额仅采集行情时间09:25至09:30且延迟不超过60秒的快照，使用供应商舍入值',
                           '净额和涨停买额缺少已验证数据源；空值不参与信号判断',
                           '历史日期仅回放已存数据；未采集的竞价数据无法用收盘行情补造']}
