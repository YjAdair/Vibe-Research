"""ML-R1 公司行动证据与总收益因子。"""
from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP
from datetime import date as date_type
from typing import Any

BASIS = 'total_return_v1_1'


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _result(raw_prev: Any, *, factor: float | None = None,
            action_day: bool = False, reason: str | None = None) -> dict:
    return {'factor': factor, 'basis': BASIS if factor is not None else None,
            'action_day': bool(action_day), 'reason': reason,
            'raw_prev_close': raw_prev}


def _events(record: Any) -> list[dict] | None:
    if record is None:
        return []
    if isinstance(record, list):
        return record if all(isinstance(x, dict) for x in record) else None
    if not isinstance(record, dict):
        return None
    if 'events' in record:
        events = record['events']
        return events if isinstance(events, list) and all(isinstance(x, dict) for x in events) else None
    if record.get('kind') in (None, 'none', '无') and not any(
            key in record for key in ('cash', 'cash_ps', 'cash_dividend', 'split_factor', 's',
                                      'bonus_ps', 'convert_ps', 'rights_issue', 'rights_ratio')):
        return []
    return [record]


def _complete(record: dict, event: dict) -> bool:
    coverage = record.get('coverage')
    complete = any(x is True for x in (
        record.get('complete'), record.get('interval_complete'),
        record.get('parse_complete'), event.get('complete'),
        event.get('interval_complete'),
        coverage.get('parse_complete') if isinstance(coverage, dict) else False,
        coverage.get('complete') if isinstance(coverage, dict) else False,
    ))
    verified = any(x is True for x in (record.get('verified'), event.get('verified')))
    return complete and verified


def _interval_complete(previous_day: str, day: str, record: Any,
                       event: dict | None = None, *, ordinary: bool = False) -> bool:
    metadata = record if isinstance(record, dict) else {}
    event = event or {}
    if any(x is True for x in (metadata.get('previous_trade_day_verified'),
                               metadata.get('verified_halt'), event.get('verified_halt'))):
        return True
    if not ordinary and any(x is True for x in (metadata.get('interval_complete'),
                                                metadata.get('cross_period_verified'),
                                                event.get('interval_complete'))):
        return True
    try:
        start = date_type.fromisoformat(previous_day)
        end = date_type.fromisoformat(day)
    except ValueError:
        return False
    gap = (end - start).days
    return gap == 1 or (gap > 1 and all(
        (start.fromordinal(n)).weekday() >= 5 for n in range(start.toordinal() + 1, end.toordinal())))


def _value(item: dict, *keys: str) -> tuple[bool, float | None]:
    for key in keys:
        if key in item:
            return True, _number(item[key])
    return False, None


def _simple_event(event: dict) -> tuple[str, float] | tuple[None, str]:
    kind = str(event.get('kind') or '').lower()
    if kind in {'rights', 'rights_issue', '配股', 'merge', 'merger', 'complex', 'other'} or any(
            event.get(key) not in (None, False, 0, 0.0, '')
            for key in ('rights_issue', 'rights_ratio', '配股', '配股比例')):
        return None, 'rights_issue_or_complex_action'

    cash_present, cash = _value(event, 'cash', 'cash_ps', 'cash_dividend')
    split_present, split = _value(event, 's', 'split_factor')
    bonus_present, bonus = _value(event, 'bonus_ps', 'bonus')
    convert_present, convert = _value(event, 'convert_ps', 'convert')
    for present, value in ((cash_present, cash), (split_present, split),
                           (bonus_present, bonus), (convert_present, convert)):
        if present and value is None:
            return None, 'incomplete_event'

    components = (bonus or 0.0) + (convert or 0.0) if (bonus_present or convert_present) else None
    if split is None and components is not None:
        split = 1.0 + components
    has_cash = cash_present and (cash or 0.0) != 0.0
    has_split = (split_present or components is not None) and split is not None and split != 1.0
    if has_cash and has_split:
        return None, 'mixed_corporate_actions'
    if has_cash:
        if cash is None or cash < 0:
            return None, 'invalid_event_values'
        if (split_present and split != 1.0) or (bonus_present and (bonus or 0) != 0) or (convert_present and (convert or 0) != 0):
            return None, 'mixed_corporate_actions'
        return 'cash', cash
    if has_split:
        if split is None or split <= 0:
            return None, 'invalid_event_values'
        if cash_present and (cash or 0.0) != 0.0:
            return None, 'mixed_corporate_actions'
        return 'split', split
    return None, 'incomplete_event'


def return_evidence(*, previous_close: Any, close: Any, official_reference: Any,
                    previous_day: Any, day: Any, record: Any = None) -> dict:
    """Return a verified total-return factor without inferring corporate actions."""
    raw = _number(previous_close)
    price = _number(close)
    official = _number(official_reference)
    if raw is None or raw <= 0 or price is None or price <= 0:
        return _result(previous_close, reason='missing_or_invalid_price')
    if not isinstance(previous_day, str) or not isinstance(day, str) or not previous_day or not day or day <= previous_day:
        return _result(previous_close, reason='invalid_date_interval')

    events = _events(record)
    if events is None:
        return _result(raw, action_day=True, reason='incomplete_event')
    in_interval = []
    for event in events:
        ex_date = event.get('ex_date')
        if not isinstance(ex_date, str) or not ex_date:
            return _result(raw, action_day=True, reason='incomplete_event')
        if previous_day < ex_date <= day:
            in_interval.append(event)
    action_day = bool(in_interval) or (official is not None and official != raw)
    if len(in_interval) > 1:
        return _result(raw, action_day=True, reason='multiple_events')

    if not in_interval:
        if official is None:
            return _result(raw, action_day=action_day, reason='missing_official_reference')
        if official != raw:
            return _result(raw, action_day=True, reason='official_reference_conflict')
        if not _interval_complete(previous_day, day, record, ordinary=True):
            return _result(raw, reason='incomplete_price_interval')
        return _result(raw, factor=price / raw)

    event = in_interval[0]
    record_dict = record if isinstance(record, dict) else {}
    if not _complete(record_dict, event) or not _interval_complete(previous_day, day, record_dict, event):
        return _result(raw, action_day=True, reason='unverified_or_incomplete_event')
    kind, value = _simple_event(event)
    if kind is None:
        return _result(raw, action_day=True, reason=value)
    if official is not None:
        raw_decimal, value_decimal = Decimal(str(raw)), Decimal(str(value))
        expected = (raw_decimal - value_decimal) if kind == 'cash' else (raw_decimal / value_decimal)
        # 只有来源明确给出价格单位与舍入规则时才按最小变动单位核对。
        tick = _number(record_dict.get('price_tick'))
        if record_dict.get('price_tick_verified') is True and record_dict.get('price_rounding') == 'half_up' and tick and tick > 0:
            unit = Decimal(str(tick))
            expected = (expected / unit).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * unit
        if Decimal(str(official)) != expected:
            return _result(raw, action_day=True, reason='official_reference_conflict')
    factor = ((price + value) / raw) if kind == 'cash' else (value * price / raw)
    if not math.isfinite(factor) or factor < 0:
        return _result(raw, action_day=True, reason='invalid_factor')
    return _result(raw, factor=factor, action_day=True)
