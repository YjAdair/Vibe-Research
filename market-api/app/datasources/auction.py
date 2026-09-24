"""Conservative opening-auction extraction from timestamped Tencent quotes.

The daily opening price remains usable after the auction. Cumulative amount and
turnover are usable only when the provider timestamp is 09:25 <= t < 09:30.
Order-book values are deliberately not mapped to proprietary net/buy amounts.
"""
from datetime import datetime, time
from math import isfinite
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')


def number(value):
    if value in (None, '') or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (ValueError, TypeError):
        return None


def limit_buy_from_quote(quote: dict) -> float | None:
    """Opening-auction limit-up buy queue value from the visible five-level book.

    During 09:25-09:30 the matching is finished; when the price sits exactly at
    the daily up-limit, the bid1 volume is the unmatched limit-up queue (hands),
    so the pending buy amount is bid1 hands * 100 * up-limit price. Returns None
    whenever price, up-limit or the bid1 entry is missing or inconsistent.
    """
    def group_value(raw):
        if not raw or not isinstance(raw, str):
            return None
        parts = raw.split(',')
        if len(parts) < 2:
            return None
        price, hands = number(parts[0]), number(parts[1])
        if price is None or hands is None or hands < 0:
            return None
        return price, hands

    up_limit = number(quote.get('up_px'))
    price = number(quote.get('price'))
    if up_limit is None or up_limit <= 0 or price is None or abs(price - up_limit) > 1e-6:
        return None
    entry = group_value(quote.get('bid_grp'))
    if not entry or entry[0] <= 0:
        return None
    if abs(entry[0] - up_limit) > 1e-6:
        return None
    return entry[1] * 100 * up_limit


def extract(quote: dict, day: str, now: datetime) -> dict | None:
    try:
        stamp = datetime.strptime(quote['timestamp'], '%Y%m%d%H%M%S').replace(tzinfo=TZ)
    except (KeyError, ValueError, TypeError):
        return None
    if stamp.date().isoformat() != day or stamp > now or stamp.time() < time(9, 25):
        return None
    def field(key):
        if quote.get('field_validity', {}).get(key) is False:
            return None
        return number(quote.get(key))
    opening, previous = field('open'), field('prev_close')
    if not opening or not previous or opening <= 0 or previous <= 0:
        return None
    row = {'date': day, 'stock_code': quote['code'], 'auction_pct': round((opening / previous - 1) * 100, 6),
           'auction_price': opening, 'prev_close': previous, 'auction_amount': None,
           'auction_turnover': None, 'up_limit_buy_amount': None, 'auction_net_amount': None,
           'daily_trade_amount': None, 'source': 'tencent_public_quote',
           'source_as_of': stamp.isoformat(), 'collected_at': now.isoformat(), 'field_sources': {}}
    row['field_sources']['auction_pct'] = {'source': row['source'], 'source_as_of': row['source_as_of'], 'basis': 'open / prev_close - 1'}
    if stamp.time() < time(9, 30):
        # Reject delayed auction snapshots fetched long after the phase ended.
        if (now - stamp).total_seconds() <= 60 and now.time() < time(9, 30):
            amount, turnover = field('amount'), field('turnover')
            if amount is not None and amount > 0:
                row['auction_amount'] = amount * 10000
            if turnover is not None and turnover >= 0:
                row['auction_turnover'] = turnover
            for key in ('auction_amount', 'auction_turnover'):
                if row[key] is not None:
                    row['field_sources'][key] = {'source': row['source'], 'source_as_of': row['source_as_of'], 'basis': '09:25–09:30 cumulative quote; rounded provider value'}
            buy = limit_buy_from_quote(quote)
            if buy is not None and buy > 0:
                row['up_limit_buy_amount'] = buy
                row['field_sources']['up_limit_buy_amount'] = {'source': row['source'], 'source_as_of': row['source_as_of'], 'basis': 'auction price at daily up-limit; bid1 unmatched hands * 100 * up-limit price'}
    elif stamp.time() >= time(15, 0):
        amount = field('amount')
        if amount is not None and amount > 0:
            row['daily_trade_amount'] = amount * 10000
            row['field_sources']['daily_trade_amount'] = {'source': row['source'], 'source_as_of': row['source_as_of'], 'basis': 'after-close cumulative amount'}
    return row
