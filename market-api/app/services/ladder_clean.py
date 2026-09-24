"""题材涨停梯队：公开池 + 收盘价清洗 → 本地预期语义。

预期（产品口径，≠原站盘中 tick）：
- 封板 = 收盘价 == 精确涨停价（有 daily_close 时）；池内冲突以收盘为准
- 炸板 = 炸板池 ∪（曾触板但收盘未封）∪（涨停池但收盘未封）
- keep_times = 分组键：连续板用 lbc；区间「N天M板」用 ct（与原站一致，如 3天2板→2）
- 缺盘中字段（fd_max、精确烂N、00:00 哨兵）留空或降级，meta 标明

数据流：
  em_up/em_broken（缺则 ths_*）→ ∩ 题材成分 → daily_close 封板校验/补漏 → 行字段
"""
from __future__ import annotations

from app.core.store import store
from app.services import pools
from app.services.plate_flow import _limit_price


def _ceil_fbt_minute(fbt) -> str:
    try:
        n = int(fbt)
    except (TypeError, ValueError):
        return '09:30'
    if n <= 0:
        return '00:00'
    hh, mm, ss = n // 10000, (n // 100) % 100, n % 100
    total = hh * 60 + mm + (1 if ss else 0)
    total = max(total, 9 * 60 + 30)
    total = min(total, 15 * 60)
    return f'{total // 60:02d}:{total % 60:02d}'


def _quotes(day: str, codes: set[str]) -> dict[str, dict]:
    if not codes:
        return {}
    return store.daily_close_by_codes(day, sorted(codes))


def _is_close_sealed(q: dict | None, code: str) -> bool | None:
    """True/False 有行情；None 无行情不可判。"""
    if not q:
        return None
    prev, close = q.get('prev_close'), q.get('close')
    name = q.get('stock_name') or ''
    if prev in (None, 0) or close in (None,):
        return None
    if 'ST' in str(name).upper():
        return None
    try:
        lim = float(_limit_price(prev, code, True))
    except Exception:
        return None
    return float(close) == lim


def _touched_limit(q: dict | None, code: str) -> bool:
    if not q:
        return False
    prev, high = q.get('prev_close'), q.get('high')
    name = q.get('stock_name') or ''
    if prev in (None, 0) or high in (None,) or 'ST' in str(name).upper():
        return False
    try:
        lim = float(_limit_price(prev, code, True))
    except Exception:
        return False
    return float(high) >= lim


def _keep_times(lbc: int, days: int, ct: int) -> int:
    """分组键 keep_times：与原站一致。

    - 区间板（N天M板，days>ct）：用 M=ct，如 3天2板 → 2
    - 连续连板：用池 lbc；缺 lbc 时若 days==ct 则用 ct
    - 至少为 1（封板行）
    """
    if days > ct >= 1:
        return ct
    if lbc >= 1:
        return lbc
    if ct >= 1 and (days == ct or days == 0):
        return ct
    return 1


def _desc_sealed(days: int, ct: int, hd: str, keep: int) -> str:
    """区间文案与连板分开：days>ct → N天M板；连续 → N连板；否则首板。"""
    if hd and hd not in ('首板',) and '天' in hd:
        try:
            head = hd.split('板')[0]
            d0 = int(head.split('天')[0])
            c0 = int(head.split('天')[-1])
            if d0 > c0:
                return f'{d0}天{c0}板'
            if d0 == c0 and c0 >= 2:
                return f'{c0}连板'
        except (ValueError, IndexError):
            pass
    if days > ct and ct >= 1:
        return f'{days}天{ct}板'
    if keep >= 2 and (days == ct or days == 0):
        return f'{keep}连板'
    if ct >= 2 and days == ct:
        return f'{ct}连板'
    return '首板'


def _type_sealed(s: dict, ths_extra: dict) -> str:
    zbc = int(s.get('zbc') or 0)
    if zbc >= 1:
        return f'烂{zbc + 1}'
    tt = s.get('_ths_limit_up_type') or ths_extra.get('limit_up_type') or ''
    if tt == '一字板':
        return '一'
    if tt == 'T字板':
        return 'T'
    return '自'


def build_ladder_rows(day: str, members: set[str]) -> dict:
    """对 members 构建梯队行。

    Returns:
      sealed, broken, pool_source, meta(清洗说明)
    """
    members = {str(c) for c in members if c}
    up_pool, brk_pool, pool_source = pools.ladder_pool_rows(day)
    ths_snap = pools.published('ths_up', day)
    ths_map = {str(s.get('code')): s for s in (ths_snap or {}).get('pool') or []}

    up_by = {str(s.get('c')): s for s in up_pool if str(s.get('c')) in members}
    br_by = {str(s.get('c')): s for s in brk_pool if str(s.get('c')) in members}

    # 候选：两池成员 ∪ 稍后收盘补漏
    candidates = set(up_by) | set(br_by)
    quotes = _quotes(day, candidates | members)

    # 收盘封板补漏：成分收盘封板但不在任一池
    supplemented = []
    for code in members:
        if code in candidates:
            continue
        sealed = _is_close_sealed(quotes.get(code), code)
        if sealed is True:
            q = quotes[code]
            candidates.add(code)
            up_by[code] = {
                'c': code,
                'n': q.get('stock_name') or '',
                'lbc': 1,
                'fbt': 0,
                'zbc': 0,
                'zttj': {'days': 0, 'ct': 0},
                'fund': 0,
                'amount': (q.get('amount') or 0),
                'tshare': 0,
                'ltsz': q.get('circulation_value') or 0,
                '_from_close_seal': True,
            }
            supplemented.append(code)

    sealed_rows = []
    broken_rows = []
    reclass_to_broken = []
    clean_notes = []

    for code in sorted(candidates):
        q = quotes.get(code)
        close_seal = _is_close_sealed(q, code)
        in_br = code in br_by
        in_up = code in up_by
        raw = br_by.get(code) or up_by.get(code) or {}

        # 清洗：有收盘价时以收盘封板为准；无行情则信池（炸板池优先）
        if close_seal is False:
            is_broken = True
            if in_up and not in_br:
                reclass_to_broken.append(code)
        elif close_seal is True:
            is_broken = False
        else:
            is_broken = in_br and not in_up if (in_br and in_up) else in_br
            if in_br and in_up:
                is_broken = True  # 双池冲突：无行情时偏炸板池

        ths_extra = ths_map.get(code) or {}
        zttj = raw.get('zttj') or {}
        days = int(zttj.get('days') or 0)
        ct = int(zttj.get('ct') or 0)
        fund_yi = round((raw.get('fund') or 0) / 1e8, 2)
        amount_raw = raw.get('amount') or 0
        # THS amount 已是元量级小数或元；EM 为元。统一 /1e8 仅当 >=1e5
        if amount_raw >= 1e5:
            amount_yi = round(amount_raw / 1e8, 2)
        else:
            amount_yi = round(float(amount_raw), 2) if amount_raw else 0.0

        if is_broken:
            keep = 0
            typ = '炸'
            if ct >= 2:
                desc = f'昨{ct}连板'
            elif close_seal is False and _touched_limit(q, code):
                desc = '触板未封'
            else:
                desc = ''
            fbt = raw.get('fbt')
            tm = _ceil_fbt_minute(fbt) if fbt not in (None, '', 0, '0') else '00:00'
            row = {
                'stock_code': code,
                'stock_name': ''.join((raw.get('n') or (q or {}).get('stock_name') or '').split()),
                'up_limit_type': typ,
                'up_limit_time': tm,
                'up_limit_desc': desc,
                'up_limit_keep_times': keep,
                'interval_limit_count': ct or None,
                'interval_days': days or None,
                'auction_turnover': 0.0,
                'checked': None,
                'market_c': round((raw.get('tshare') or 0) / 1e8, 2),
                'market_c_c': round((raw.get('ltsz') or (q or {}).get('circulation_value') or 0) / 1e8, 2),
                'fd_max': None,  # 免费源无盘中最大封单
                'fd_close': 0.0,
                'amount': amount_yi,
                'next_day': None,
                'next_open_pct': None,
                'next_close_pct': None,
                '_clean': 'broken',
            }
            broken_rows.append(row)
        else:
            lbc = int(raw.get('lbc') or 0)
            keep = _keep_times(lbc, days, ct)
            hd = raw.get('_ths_high_days') or ths_extra.get('high_days') or ''
            desc = _desc_sealed(days, ct, hd, keep)
            typ = _type_sealed(raw, ths_extra)
            if raw.get('_from_close_seal'):
                tm = '--'
                typ = typ if typ != '自' else '自'
                clean_notes.append(f'{code}:close_seal_supplement')
            else:
                tm = _ceil_fbt_minute(raw.get('fbt'))
            row = {
                'stock_code': code,
                'stock_name': ''.join((raw.get('n') or (q or {}).get('stock_name') or '').split()),
                'up_limit_type': typ,
                'up_limit_time': tm,
                'up_limit_desc': desc,
                'up_limit_keep_times': keep,
                'interval_limit_count': ct if ct else None,
                'interval_days': days if days else None,
                'auction_turnover': 0.0,
                'checked': None,
                'market_c': round((raw.get('tshare') or 0) / 1e8, 2),
                'market_c_c': round((raw.get('ltsz') or (q or {}).get('circulation_value') or 0) / 1e8, 2),
                'fd_max': fund_yi if fund_yi else None,  # 仅有收盘封单时不冒充 max
                'fd_close': fund_yi,
                'amount': amount_yi,
                'next_day': None,
                'next_open_pct': None,
                'next_close_pct': None,
                '_clean': 'close_seal_supplement' if raw.get('_from_close_seal') else 'pool_sealed',
            }
            sealed_rows.append(row)

    sealed_rows.sort(key=lambda x: (-x['up_limit_keep_times'], x['up_limit_time'] or '99', x['stock_code']))
    broken_rows.sort(key=lambda x: (x['up_limit_time'] or '99', x['stock_code']))

    meta = {
        'pool_source': pool_source,
        'seal_rule': 'daily_close_equals_limit_price',
        'supplemented_close_seal': supplemented,
        'reclassified_to_broken': reclass_to_broken,
        'notes': clean_notes,
        'field_limits': {
            'fd_max': 'unavailable_free_source',
            'up_limit_type_lan': 'eastmoney_zbc_eod_approx',
            'broken_time_00': 'unknown_or_unsealed_sentinel',
        },
    }
    return {
        'sealed': sealed_rows,
        'broken': broken_rows,
        'pool_source': pool_source,
        'meta': meta,
    }
