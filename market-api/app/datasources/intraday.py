"""Tencent minute records: HHMM price cumulative-volume(hands) cumulative-amount(yuan)."""
from datetime import datetime, time
from app.datasources.auction import number


def parse_session(day: str, raw: list[str], previous_close=None, *, stock=True) -> dict:
    date = datetime.strptime(day.replace('-', ''), '%Y%m%d').date().isoformat()
    rows, excluded = [], []
    previous_volume = previous_amount = 0
    last_time = None
    for item in raw:
        parts = str(item).split()
        if stock and len(parts) != 4:
            raise ValueError('Invalid minute record shape')
        if not stock and len(parts) not in (3, 4):
            raise ValueError('Invalid index minute record shape')
        minute = datetime.strptime(parts[0], '%H%M').time()
        if not (time(9,30) <= minute <= time(11,30) or time(13) <= minute <= time(15)):
            excluded.append(item)
            continue
        price = number(parts[1])
        hands = number(parts[2]) if len(parts) > 2 else None
        amount = number(parts[3]) if len(parts) > 3 else None
        if price is None or price <= 0:
            raise ValueError('Invalid minute number')
        if hands is not None and hands < 0:
            raise ValueError('Invalid minute number')
        if amount is not None and amount < 0:
            raise ValueError('Invalid minute number')
        if last_time is not None and minute <= last_time:
            raise ValueError('Duplicate or out-of-order minute')
        if hands is not None and previous_volume is not None and hands < previous_volume:
            raise ValueError('Cumulative totals decreased')
        if amount is not None and previous_amount is not None and amount < previous_amount:
            raise ValueError('Cumulative totals decreased')
        volume = hands * 100 if stock and hands is not None else None
        avg = round(amount/volume,6) if stock and volume and amount is not None else None
        rows.append({'time':minute.strftime('%H:%M'), 'price':price,
                     'average_price':avg,
                     'cumulative_volume':volume, 'cumulative_amount':amount,
                     'volume':(hands-previous_volume)*100 if stock and hands is not None and previous_volume is not None else None,
                     'amount':(amount-previous_amount) if amount is not None and previous_amount is not None else None,
                     'interval_start':last_time.strftime('%H:%M') if last_time else None,
                     'first_interval_includes_opening_auction':not rows})
        last_time, previous_volume, previous_amount = minute, hands, amount
    if not rows:
        raise ValueError('No regular-session minute records')
    prev = number(previous_close)
    for row in rows:
        row['pct'] = round((row['price']/prev-1)*100,6) if prev and prev>0 else None
    expected = [f'{m//60:02d}:{m%60:02d}' for start,end in ((570,690),(780,900)) for m in range(start,end+1)]
    observed = {row['time'] for row in rows}
    # Only elapsed time up to the last reported record is checked during trading.
    missing = [t for t in expected if t <= rows[-1]['time'] and t not in observed]
    return {'date':date,'points':rows, 'prev_close':prev if prev and prev>0 else None,
            'missing_times':missing, 'full_day_points':len(expected),
            'quality_status':'gaps' if missing else 'observed_contiguous',
            'source_as_of':date+'T'+rows[-1]['time']+':00+08:00',
            'excluded_count':len(excluded), 'excluded_rows':excluded,
            'volume_unit':'shares' if stock else 'unavailable', 'amount_unit':'yuan',
            'average_basis':'cumulative_amount / cumulative_volume; vendor rounded volume' if stock else 'not_applicable',
            'interval_basis':'difference from preceding available record; first point includes opening auction',
            'raw_rows':raw}
