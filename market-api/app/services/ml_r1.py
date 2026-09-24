"""ML-R1确定性计算：只消费带时点证据的独立输入，不联网。内部收益为小数。"""
from __future__ import annotations
import math
from datetime import datetime
from statistics import median
from zoneinfo import ZoneInfo

VERSION = 'ml_r1_v1_1'
SOURCE_ALLOWLIST = {'eastmoney_public', 'eastmoney_native_v1', 'local_independent'}
WEIGHTS = {'relative_return': .4, 'relative_breadth': .3, 'volume_confirmation': .2, 'limit_diffusion': .1}
TZ = ZoneInfo('Asia/Shanghai')


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def known_before(value, cutoff):
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return False
        return stamp <= datetime.fromisoformat(cutoff).replace(tzinfo=TZ)
    except (ValueError, TypeError):
        return False


def verified_close_time(value, date):
    """逐证券盘后时点；调用方不能用整批max时间代替每条行情证据。"""
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        local = stamp.astimezone(TZ)
        return (stamp.tzinfo is not None and local.date().isoformat() == date
                and local.hour >= 15 and stamp <= datetime.now(TZ))
    except (ValueError, TypeError):
        return False


def total_return(row, action=None):
    factor = number(row.get('total_return_factor'))
    if row.get('total_return_verified') is True and factor is not None and factor >= 0:
        return factor - 1
    close, prev = number(row.get('close')), number(row.get('prev_close'))
    if close is None or close < 0 or prev is None or prev <= 0:
        return None
    action = action if action is not None else row.get('corporate_action')
    if not isinstance(action, dict) or action.get('verified') is not True or action.get('complex'):
        return None
    if 'split_factor' in action and 'cash_dividend' in action:
        return None  # 混合行动必须提供经验证总收益因子。
    if 'split_factor' in action:
        split = number(action['split_factor'])
        return split * close / prev - 1 if split is not None and split > 0 else None
    if 'cash_dividend' in action:
        cash = number(action['cash_dividend'])
        return (close + cash) / prev - 1 if cash is not None and cash >= 0 else None
    return close / prev - 1 if action.get('kind') == 'none' else None


def compound(values):
    xs = [number(x) for x in values]
    if not xs or any(x is None or x < -1 for x in xs):
        return None
    return math.prod(1 + x for x in xs) - 1


def mean(values):
    xs = [number(x) for x in values]
    return sum(xs) / len(xs) if xs and all(x is not None for x in xs) else None


def total(values):
    xs = [number(x) for x in values]
    return sum(xs) if xs and all(x is not None for x in xs) else None


def mad_z(values):
    if not values:
        return [], 0., True
    center = median(values)
    mad = median(abs(x - center) for x in values)
    if mad <= 1e-12:
        return [0.] * len(values), mad, True
    return [max(-3., min(3., (x - center) / (1.4826 * mad))) / 3 for x in values], mad, False


def strength_contributions(factors):
    if any(number(factors.get(k)) is None or abs(factors[k]) > 1 for k in WEIGHTS):
        return None
    return {k: 100 * w * factors[k] for k, w in WEIGHTS.items()}


def relative_volume(row, prior_days):
    amount = number(row.get('amount'))
    history = row.get('amount_history', [])
    dates = row.get('amount_history_dates', [])
    if len(prior_days) != 20 or dates != prior_days or len(history) != 20:
        return None
    xs = [number(x) for x in history]
    if amount is None or amount < 0 or any(x is None or x < 0 for x in xs):
        return None
    base = sum(xs) / 20
    return amount / base if base > 0 else None


def _group(members, date, prior_days, actions):
    """未知状态仍在覆盖率分母；明确停牌/上市不足20日从活跃分母剔除。"""
    excluded, eligible = [], []
    seen = set()
    for r in members:
        code = r.get('code')
        if not code or code in seen:
            return {'reasons': ['duplicate_or_missing_stock_code'], 'coverage': {}, 'status': 'missing_input'}
        seen.add(code)
        listed = number(r.get('listed_days'))
        if r.get('halted') is True or (listed is not None and listed < 20):
            excluded.append(code)
        else:
            eligible.append(r)
    n = len(eligible)
    valid_rows = [r for r in eligible if r.get('tradable') is True and r.get('halted') is False
                  and number(r.get('listed_days')) is not None and r.get('trade_date') == date]
    def dated_return(r):
        action = actions.get(r['code'], r.get('corporate_action'))
        if r.get('total_return_verified') is True and r.get('total_return_date') != date:
            return None
        if isinstance(action, dict) and action.get('kind') != 'none' and action.get('ex_date') != date:
            return None
        return total_return(r, action)
    returns = {r['code']: dated_return(r) for r in valid_rows}
    prices = [v for v in returns.values() if v is not None]
    volumes = [relative_volume(r, prior_days) for r in valid_rows]
    volumes = [v for v in volumes if v is not None]
    limited = [r for r in valid_rows if r.get('limit_applicable') is True]
    limits = [r for r in limited if type(r.get('close_limit_up')) is bool and type(r.get('close_limit_down')) is bool
              and not (r['close_limit_up'] and r['close_limit_down'])]
    regime_known = sum(type(r.get('limit_applicable')) is bool for r in valid_rows)
    coverage = {'member_total': len(members), 'eligible': n, 'excluded': len(excluded),
                'valid_price': len(prices), 'valid_volume': len(volumes), 'limit_eligible': len(limited),
                'valid_limit': len(limits), 'price': len(prices) / n if n else 0,
                'volume': len(volumes) / n if n else 0, 'limit': len(limits) / len(limited) if limited else 0,
                'regime': regime_known / n if n else 0}
    rs = {'coverage': coverage, 'reasons': [], 'status': 'missing_input', 'excluded_codes': excluded,
          'return': None, 'breadth': None, 'rv': None, 'limit_diffusion': None, 'member_returns': {}}
    if len(prices) >= 5 and coverage['price'] >= .95:
        rs['return'] = mean(prices)
        rs['breadth'] = (sum(x > 0 for x in prices) - sum(x < 0 for x in prices)) / len(prices)
        rs['member_returns'] = {r['code']: {'return': returns[r['code']], 'stock_name': r.get('name', r['code'])}
                                for r in valid_rows if returns[r['code']] is not None}
    else:
        rs['reasons'].append('price_coverage_below_95_percent')
    if coverage['volume'] >= .95:
        rs['rv'] = median(volumes)
    else:
        rs['reasons'].append('missing_20_day_volume')
    if coverage['regime'] == 1 and limited and coverage['limit'] >= .95:
        rs['limit_diffusion'] = (sum(r['close_limit_up'] for r in limits) - sum(r['close_limit_down'] for r in limits)) / len(limits)
    else:
        rs['reasons'].append('missing_limit_diffusion')
    quote_returns = [number(r.get('quote_return')) for r in valid_rows]
    quote_values = [v for v in quote_returns if v is not None]
    rs['quote_rate'] = mean(quote_values) * 100 if n and len(quote_values) == n else None
    rs['quote_coverage'] = {'valid': len(quote_values), 'expected': n}
    rs['up_count'] = sum(v > 0 for v in prices) if coverage['price'] == 1 and n else None
    rs['down_count'] = sum(v < 0 for v in prices) if coverage['price'] == 1 and n else None
    rs['up_ratio'] = rs['up_count'] / n * 100 if rs['up_count'] is not None else None
    rs['limit_up_count'] = sum(r['close_limit_up'] for r in limits) if coverage['regime'] == 1 and coverage['limit'] == 1 else None
    rs['limit_down_count'] = sum(r['close_limit_down'] for r in limits) if coverage['regime'] == 1 and coverage['limit'] == 1 else None
    amounts = [number(r.get('amount')) for r in eligible]
    rs['trade_money'] = total(amounts) if len(valid_rows) == n and all(x is not None and x >= 0 for x in amounts) else None
    # 资金只按完整成员与统一供应商口径累加，不从净值反推买卖。
    flow_ok = eligible and len(valid_rows) == n and all(r.get('flow_definition') == 'eastmoney_large_v1'
                              and r.get('flow_verified') is True and r.get('flow_date') == date
                              and number(r.get('leader_buy')) is not None and r['leader_buy'] >= 0
                              and number(r.get('leader_sell')) is not None and r['leader_sell'] >= 0 for r in eligible)
    rs['money_leader_buy'] = total([r['leader_buy'] for r in eligible]) if flow_ok else None
    rs['money_leader_sell'] = total([r['leader_sell'] for r in eligible]) if flow_ok else None
    rs['leader_money'] = rs['money_leader_buy'] - rs['money_leader_sell'] if flow_ok else None
    definitions = {r.get('flow_definition') for r in eligible}
    net_ok = (bool(eligible) and len(valid_rows) == n and len(definitions) == 1
              and next(iter(definitions)) in {'eastmoney_large_v1', 'eastmoney_push2delay_f62'}
              and all(r.get('flow_verified') is True and r.get('flow_date') == date
                      and number(r.get('main_net_inflow')) is not None for r in eligible))
    if net_ok:
        rs['leader_money'] = total([r['main_net_inflow'] for r in eligible])
    rs['money_leader'] = rs['leader_money']
    rs['flow_definition'] = next(iter(definitions)) if net_ok else 'eastmoney_large_v1' if flow_ok else None
    rs['money_strength'] = rs['leader_money'] / rs['trade_money'] if (flow_ok or net_ok) and rs['trade_money'] else None
    rs['status'] = 'final' if not rs['reasons'] and all(coverage[k] == 1 for k in ('price', 'volume', 'limit', 'regime')) else 'partial_preview' if not rs['reasons'] else 'missing_input'
    return rs


def compute_day(*, date, topics, market, actions=None, min_topics=20, source=None, source_evidence=None,
                market_full_universe=False, market_known_at=None, prior_days=None, catalog_complete=False, **kwargs):
    actions, prior_days = actions or {}, prior_days or []
    reasons = []
    if source not in SOURCE_ALLOWLIST or not source_evidence:
        reasons.append('source_not_allowlisted_or_unproven')
    if not market_full_universe or not known_before(market_known_at, date + 'T09:30:00'):
        reasons.append('missing_market_universe')
    if len(prior_days) != 20 or prior_days != sorted(set(prior_days)) or any(d >= date for d in prior_days):
        reasons.append('missing_20_day_calendar')
    if not catalog_complete:
        reasons.append('incomplete_taxonomy_catalog')
    market_result = _group(market, date, prior_days, actions)
    if market_result['status'] == 'missing_input':
        reasons.extend('market_' + r for r in market_result['reasons'])
    raw = []
    for topic in topics:
        r = {'plate_code': str(topic['code']), 'plate_name': topic.get('name', topic['code'])}
        member_ok = topic.get('membership_verified') is True and known_before(topic.get('membership_known_at'), date + 'T09:30:00') and str(topic.get('effective_from', '9999')) <= date and (not topic.get('effective_to') or date < topic['effective_to'])
        if not member_ok:
            r.update(status='missing_input', reasons=['missing_point_in_time_membership'], coverage={})
        else:
            r.update(_group(topic.get('members', []), date, prior_days, actions))
            r['membership_as_of'] = topic.get('membership_known_at')
        r.update(strength=None, factors={}, contributions={}, formula_version=VERSION)
        xs = {k: None for k in WEIGHTS}
        if r.get('return') is not None and market_result.get('return') is not None:
            xs['relative_return'] = r['return'] - market_result['return']
        if r.get('breadth') is not None and market_result.get('breadth') is not None:
            xs['relative_breadth'] = r['breadth'] - market_result['breadth']
        if r.get('rv') is not None and market_result.get('rv') and xs['relative_return'] is not None:
            sign = (xs['relative_return'] > 0) - (xs['relative_return'] < 0)
            xs['volume_confirmation'] = sign * math.log1p(r['rv'] / market_result['rv'])
        if r.get('limit_diffusion') is not None and market_result.get('limit_diffusion') is not None:
            xs['limit_diffusion'] = r['limit_diffusion'] - market_result['limit_diffusion']
        r['x'] = xs
        raw.append(r)
    qualified = [r for r in raw if all(v is not None for v in r['x'].values()) and r['status'] != 'missing_input']
    if len(qualified) < max(20, min_topics):
        reasons.append('cross_section_below_20_topics')
    if len(qualified) / max(1, len(raw)) < .8:
        reasons.append('cross_section_below_80_percent')
    if len({r['plate_code'] for r in raw}) != len(raw):
        reasons.append('duplicate_topic_code')
    if not reasons:
        section_final = len(qualified) == len(raw) and all(r['status'] == 'final' for r in qualified)
        for key in WEIGHTS:
            values, mad, degenerate = mad_z([r['x'][key] for r in qualified])
            for r, value in zip(qualified, values):
                r['factors'][key] = {'x': r['x'][key], 'f': value, 'mad': mad, 'degenerate_factor': degenerate, 'valid_topics': len(qualified)}
        for r in qualified:
            r['contributions'] = strength_contributions({k: f['f'] for k, f in r['factors'].items()})
            r['strength'] = sum(r['contributions'].values())
            if market_result['status'] != 'final' or not section_final:
                r['status'] = 'partial_preview'
    else:
        for r in raw:
            r['reasons'] = list(dict.fromkeys(r['reasons'] + reasons))
            r['status'] = 'missing_input'
    for r in raw:
        c = r.get('coverage', {})
        r['metric_status'] = {'strength': r['status'] if r.get('strength') is not None else 'missing_input',
            'return': ('final' if c.get('price') == 1 else 'partial_preview') if r.get('return') is not None else 'missing_input',
            'money': 'final' if r.get('leader_money') is not None else 'missing_input'}
    return {'date': date, 'status': 'final' if not reasons and all(r['status'] == 'final' for r in raw) else 'partial_preview' if not reasons else 'missing_input',
            'formula_id': 'ml_r1', 'formula_version': VERSION, 'source': source, 'source_evidence': source_evidence,
            'source_as_of': kwargs.get('source_as_of'), 'reasons': list(dict.fromkeys(reasons)), 'rows': raw,
            'coverage': {'topic_total': len(raw), 'qualified_topics': len(qualified), 'qualified_ratio': len(qualified) / max(1, len(raw)), 'market': market_result.get('coverage', {})}}


def tiers(close_limit_up, window=6, previous_ladder=None):
    history = close_limit_up[-window:]
    count = sum(x is True for x in history) if len(history) == window and all(type(x) is bool for x in history) else None
    consecutive = None
    if close_limit_up:
        if close_limit_up[-1] is False:
            consecutive = 0
        elif close_limit_up[-1] is True:
            if previous_ladder is not None and previous_ladder >= 0:
                consecutive = previous_ladder + 1
            else:
                n = 0
                for v in reversed(close_limit_up):
                    if v is True:
                        n += 1
                    elif v is False:
                        consecutive = n
                        break
                    else:
                        break
    return {'consecutive': consecutive, 'window': window, 'count': count, 'status': 'final' if consecutive is not None and count is not None else 'missing_input'}


def popularity_change(previous, current):
    a, b = number(previous), number(current)
    return a - b if a is not None and b is not None and a >= 1 and b >= 1 else None


def pct_bucket(value):
    value = number(value)
    if value is None or value < 20:
        return None
    return '20-40' if value < 40 else '40-60' if value < 60 else '60-80' if value < 80 else '80-100' if value < 100 else '100+'


compound_topic_daily = compound


def limit_price(reference, rate, tick='0.01', *, minimum_move=False):
    """按已验证、对应日期的涨跌幅制度计算价格；不在此猜测ST或上市板块制度。"""
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
    try:
        ref, pct, step = map(lambda x: Decimal(str(x)), (reference, rate, tick))
        if not all(v.is_finite() for v in (ref, pct, step)) or ref <= 0 or step <= 0 or pct <= -1:
            return None
        price = ((ref * (1 + pct)) / step).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * step
        if pct != 0 and abs(price - ref) < step:
            if not minimum_move:
                return None  # 未核验低价例外，不能生成涨跌停重合的价格。
            price = ref + (step if pct > 0 else -step)
        if minimum_move:
            price = max(step, price)
        return float(price) if price > 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def limit_state(close, high, upper, lower):
    values = [number(v) for v in (close, upper, lower)]
    if any(v is None for v in values) or not values[2] <= values[0] <= values[1]:
        return {'close_limit_up': None, 'close_limit_down': None, 'broken_limit_up': None}
    h = number(high)
    return {'close_limit_up': values[0] == values[1], 'close_limit_down': values[0] == values[2],
            'broken_limit_up': h >= values[1] and values[0] < values[1] if h is not None and h >= values[0] else None}
