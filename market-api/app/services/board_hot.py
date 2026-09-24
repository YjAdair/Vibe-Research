"""复盘板块涨停热度：只读已发布涨停池/炸板池/板块快照。"""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from app.core.store import store
from app.services import pools

TZ = ZoneInfo('Asia/Shanghai')


def _hm(value) -> str:
    if value in (None, '', 0, '0'):
        return ''
    if isinstance(value, str) and ':' in value:
        return value[:5]
    try:
        n = int(value)
        if n > 1000000000:
            from time import localtime, strftime
            return strftime('%H:%M', localtime(n))
        return f'{n // 10000:02d}:{n // 100 % 100:02d}'
    except (TypeError, ValueError):
        return ''


def _type_of(open_num: int, failed: bool) -> str:
    if failed:
        return '炸'
    if open_num > 1:
        return f'烂{open_num}'
    if open_num == 1:
        return 'T'
    return '一'


def _exact_limit_up_price(prev_close: float, code: str, name: str) -> float | None:
    """精确涨停价：与 intraday._zt_price_exact 同口径（先半入昨收到分再乘系数）。

    主板 10%（系数 1.10），创业板/科创板 20%（系数 1.20）；ST 不计，北交所不在池内。
    """
    if 'ST' in str(name).upper():
        return None
    if not str(code).startswith(('30', '68', '00', '60')):
        return None
    from decimal import Decimal, ROUND_HALF_UP
    p = Decimal(str(prev_close)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    coef = Decimal('1.20') if str(code).startswith(('30', '68')) else Decimal('1.10')
    return float((p * coef).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


async def review(date1: str | None = None, plate_type: str = 'concept') -> dict:
    requested = date1.replace('-', '') if date1 else None
    today = datetime.now(TZ).strftime('%Y%m%d')
    day = requested or today
    iso = f'{day[:4]}-{day[4:6]}-{day[6:]}'
    up = pools.published('up', iso, allow_stale=not bool(date1))
    if up is None and settings.collector_mode == 'embedded':
        try:
            up = await pools.require('up', iso, allow_stale=not bool(date1))
        except Exception:
            up = None
    if up is None or (requested and up['date'].replace('-', '') != requested):
        return {'date': day, 'today': day == today, 'plate': [], 'plate_stocks': {}, 'plate_stocks_zb': {},
                'ban_info': {}, 'stocks': '', 'status': 'missing', 'source': 'published_limit_pools'}
    day = up['date'].replace('-', '')
    iso = up['date']
    broken = pools.published('broken', iso) or {'pool': []}
    ths_up = pools.published('ths_up', iso) or {'pool': []}
    ths_map = {s.get('code'): s for s in ths_up.get('pool') or []}
    calendar = store.kv_get('collector_calendar_v1', {}) or {}
    days = [str(x)[:10] if '-' in str(x) else f"{str(x)[:4]}-{str(x)[4:6]}-{str(x)[6:]}" for x in (calendar.get('days') or [])]
    next_iso = None
    if iso in days:
        idx = days.index(iso)
        if idx + 1 < len(days):
            next_iso = days[idx + 1]
    codes = [str(s.get('c') or '') for s in up.get('pool') or [] if s.get('c')]
    next_map = store.daily_close_by_codes(next_iso, codes) if next_iso else {}
    block_top = store.block_top_get(iso) or {}
    articles = {}
    for plate in block_top.get('items') or []:
        for row in plate.get('stock_list') or []:
            code = str(row.get('code') or '')
            if code and (code not in articles or len(row.get('reason_info') or '') > len(articles[code].get('reason_info') or '')):
                articles[code] = row
    board_type = 3 if plate_type != 'industry' else 2
    boards, _ = store.board_snapshot_range(board_type, iso, iso)
    board_by_name = {b['plate_name']: b for b in boards}
    base_by_name = {b['plate_name'].rstrip('ⅡⅢ'): b for b in boards}

    def board_of(name: str):
        return board_by_name.get(name) or base_by_name.get((name or '').rstrip('ⅡⅢ'))

    groups = defaultdict(list)
    concepts = up.get('concepts_by_code') or {}
    for s in up.get('pool') or []:
        code = str(s.get('c') or '')
        names = concepts.get(code) or ([s.get('hybk')] if s.get('hybk') else [])
        if plate_type == 'industry':
            names = [s.get('hybk')] if s.get('hybk') else []
        extra = ths_map.get(code) or {}
        nxt = next_map.get(code) or {}
        prev_close = nxt.get('prev_close')
        next_open_pct = next_close_pct = None
        next_is_limit_up = False
        if prev_close not in (None, 0) and nxt.get('open') not in (None, 0):
            next_open_pct = round((float(nxt['open']) / float(prev_close) - 1) * 100, 2)
        if prev_close not in (None, 0) and nxt.get('close') not in (None, 0):
            next_close_pct = round((float(nxt['close']) / float(prev_close) - 1) * 100, 2)
            # 晋级 = 次日收盘达到精确涨停价（主板 10%、创业板/科创板 20%，ST/北交所不入池）。
            # 旧的固定阈值 next_close_pct >= 9.5 对 20cm 股票口径错误，此处按板块规则精确判定。
            limit_price = _exact_limit_up_price(float(prev_close), code, str(s.get('n') or ''))
            next_is_limit_up = limit_price is not None and abs(float(nxt['close']) - limit_price) < 0.005
        article = articles.get(code) or {}
        entry = {
            'stock_code': code,
            'stock_name': s.get('n', ''),
            'up_limit_keep_times': int(s.get('lbc') or extra.get('lbc') or 1),
            'up_limit_type': _type_of(int(extra.get('open_num') or 0), False),
            'up_limit_time': _hm(s.get('fbt') or extra.get('first_limit_up_time')),
            'up_limit_desc': extra.get('high_days') or article.get('high') or '',
            'reason_info': article.get('reason_info') or extra.get('reason') or '',
            'reason': extra.get('reason') or article.get('reason_type') or '',
            'amount': s.get('amount'),
            'fd_max': s.get('fund'),
            'open_num': extra.get('open_num') or 0,
            'next_day': next_iso,
            'next_open_pct': next_open_pct,
            'next_close_pct': next_close_pct,
            'next_is_limit_up': next_is_limit_up,
            'fengdan_money': s.get('fund'),
            'actualcirculation_value': s.get('ltsz'),
            'turnover_ration_real': s.get('hs'),
            'stock_price': round(float(s.get('p') or 0) / 1000, 2) if s.get('p') else extra.get('price'),
        }
        for name in [n for n in names if n]:
            groups[name].append(entry)

    ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))[:18]
    plate, plate_info, plate_stocks, plate_stocks_zb, stock_info = [], {}, {}, {}, {}
    stocks_all = []
    for name, rows in ranked:
        board = board_of(name) or {'plate_code': name, 'plate_name': name}
        code = board['plate_code']
        score = len(rows) * 1000
        plate.append([name, code, score])
        plate_info[code] = {'score': score, 'name': name, 'code': code}
        tagged = []
        for row in rows:
            item = dict(row, plate_code=code)
            tagged.append(item)
            stocks_all.append(row['stock_code'])
            stock_info.setdefault(row['stock_code'], {'plates': []})['plates'].append(name)
        plate_stocks[code] = tagged

    code_by_name = {p[0]: p[1] for p in plate}
    for s in broken.get('pool') or []:
        code = str(s.get('c') or s.get('code') or '')
        names = [s.get('hybk')] if plate_type == 'industry' and s.get('hybk') else ((up.get('concepts_by_code') or {}).get(code) or [])
        extra = ths_map.get(code) or {}
        for name in names:
            pk = code_by_name.get(name)
            if not pk:
                continue
            plate_stocks_zb.setdefault(pk, []).append({
                'stock_code': code,
                'stock_name': s.get('n') or s.get('name') or '',
                'up_limit_type': '炸',
                'up_limit_keep_times': 0,
                'up_limit_time': _hm(s.get('fbt') or extra.get('first_limit_up_time')),
                'plate_code': pk,
                'open_num': extra.get('open_num') or s.get('zbc') or 0,
            })
            stocks_all.append(code)

    ban_info = {}
    for rows in plate_stocks.values():
        for row in rows:
            h = str(int(row.get('up_limit_keep_times') or 1))
            ban_info.setdefault(h, {'count': 0})
            ban_info[h]['count'] += 1
    zb_total = sum(len(v) for v in plate_stocks_zb.values())
    if zb_total:
        ban_info['0'] = {'count': zb_total}

    prev_days = [x for x in (store.kv_get('collector_calendar_v1', {}) or {}).get('days') or [] if str(x).replace('-','') < day]
    promotion = {}
    if prev_days:
        prev = pools.published('up', prev_days[-1])
        today_codes = {str(s.get('c')) for s in up.get('pool') or []}
        by_h = defaultdict(list)
        for s in (prev or {}).get('pool') or []:
            by_h[int(s.get('lbc') or 1)].append(str(s.get('c')))
        for h, cs in by_h.items():
            if cs:
                promotion[str(h - 1)] = round(sum(1 for c in cs if c in today_codes) / len(cs) * 100)

    return {
        'today': day == today,
        'date': day,
        'status': 'ok',
        'plate': plate,
        'plate_info': plate_info,
        'plate_stocks': plate_stocks,
        'plate_stocks_zb': plate_stocks_zb,
        'plate_stocks_bx': {k: [] for k in plate_stocks},
        'stocks': ','.join(dict.fromkeys(stocks_all)),
        'max_count': max((int(k) for k in ban_info), default=0),
        'relay': {'area': [{'p_code': p[1], 'p_score': p[2], 'count': len(plate_stocks.get(p[1], [])),
                            'ban_n': max((int(x.get('up_limit_keep_times') or 0) for x in plate_stocks.get(p[1], [])), default=0)}
                           for p in plate[:8]], 'promotion': promotion},
        'plate_stocks_next_yi': {},
        'stock_info': stock_info,
        'stocks_hot': {},
        'stocks_hot_n': 0,
        'ban_info': ban_info,
        'source': 'published_limit_pools+board_snapshots',
        'limitations': ['人气榜未纳入已发布快照，保持为空', '炸板归属只使用采集时保存的概念/行业，不再按请求反查', '次日开收盘只读已发布 daily_close，缺日保持 null'],
        'next_day': next_iso,
        'block_top_status': 'ok' if block_top else 'missing',
    }


def _desc_of(keep: int) -> str:
    keep = int(keep or 1)
    return '首板' if keep <= 1 else f'{keep}连板'


async def reason_groups(date1: str | None = None, page: int = 1, page_size: int = 20) -> dict:
    """原站 /v3/api/review/uplimit/reason：按板块分组的涨停原因长文。"""
    data = await review(date1, 'concept')
    if data.get('status') == 'missing':
        return {'date': data.get('date'), 'items': [], 'total': 0, 'status': 'missing', 'source': data.get('source')}
    groups = []
    for name, code, score in data.get('plate') or []:
        stocks = []
        for s in (data.get('plate_stocks') or {}).get(code) or []:
            keep = int(s.get('up_limit_keep_times') or 1)
            stocks.append({
                'date1': data.get('date') if '-' in str(data.get('date')) else f"{str(data.get('date'))[:4]}-{str(data.get('date'))[4:6]}-{str(data.get('date'))[6:]}",
                'plate_code': code,
                'plate_name': name,
                'plate_score': score,
                'stock_code': s.get('stock_code'),
                'stock_name': s.get('stock_name'),
                'stock_price': s.get('stock_price'),
                'up_limit_keep_times': keep,
                'up_limit_desc': s.get('up_limit_desc') or _desc_of(keep),
                'up_limit_type': s.get('up_limit_type'),
                'up_limit_time': s.get('up_limit_time'),
                'reason': s.get('reason_info') or s.get('reason') or '',
                'fengdan_money': s.get('fengdan_money') or s.get('fd_max'),
                'actualcirculation_value': s.get('actualcirculation_value'),
                'turnover_ration_real': s.get('turnover_ration_real'),
                'amount': round(float(s.get('amount') or 0) / 1e8, 2) if s.get('amount') else None,
            })
        groups.append({'plate_code': code, 'plate_name': name, 'plate_score': score, 'stocks': stocks})
    total = len(groups)
    start = max(page - 1, 0) * max(page_size, 1)
    items = groups[start:start + max(page_size, 1)]
    iso = data.get('date')
    if iso and '-' not in str(iso):
        iso = f"{iso[:4]}-{iso[4:6]}-{iso[6:]}"
    return {
        'date': iso,
        'items': items,
        'total': total,
        'page': page,
        'page_size': page_size,
        'status': 'ok',
        'source': 'published_limit_pools+block_top',
        'block_top_status': data.get('block_top_status'),
        'note': 'long reason_info comes from published THS block_top; missing snapshot falls back to short reason_type',
    }
