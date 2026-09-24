"""沪深交易所严重异常波动：10日累计偏离100%、30日累计偏离200%。"""
from __future__ import annotations
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import settings
from app.core.store import store
from app.datasources.codes import market_of, normalize_code
from app.services import index_history

TZ = ZoneInfo('Asia/Shanghai')
T1_WINDOW, T2_WINDOW = 10, 30
T1_LIMIT, T2_LIMIT = 100.0, 200.0
LOOKBACK_DAYS = 40
NEAR_PCT = 55.0
ST_PREFIX = ('*ST', 'ST', 'S*ST', 'SST')


def iso(day: str) -> str:
    d = day.replace('-', '')
    if len(d) != 8 or not d.isdigit():
        raise ValueError('Invalid trade date')
    return f'{d[:4]}-{d[4:6]}-{d[6:]}'


def board_of(code: str, name: str = '') -> str | None:
    code = normalize_code(code)
    if code.startswith('68'):
        return 'kcb'
    if code.startswith('30'):
        return 'cyb'
    if market_of(code) == 'sh' and code.startswith(('60', '601', '603', '605')):
        return 'sh_main'
    if market_of(code) == 'sz' and code.startswith(('00', '001', '002', '003')):
        return 'sz_main'
    if market_of(code) == 'bj':
        return 'bj'
    return None


INDEX_FOR_BOARD = {
    'sh_main': '000002',
    'sz_main': '399107',
    'cyb': '399102',
    'kcb': '000680',
    'bj': '899050',
}


def _pct(end: float, start: float) -> float:
    return round((end / start - 1) * 100, 2)


def _threshold_price(start: float, index_end: float, index_start: float, limit: float) -> float:
    return round(start * (1 + limit / 100) * (index_end / index_start), 2)


def _best_window(closes: list[dict], indexes: list[dict], end: str, width: int, limit: float) -> dict | None:
    """Choose the start date inside the last `width` sessions that maximizes stock-minus-index deviation."""
    by_day = {r['trade_date']: r for r in closes if r.get('close') and r.get('prev_close')}
    idx = {r['trade_date']: r for r in indexes if r.get('close')}
    dates = [d for d in sorted(set(by_day) & set(idx)) if d <= end]
    if end not in by_day or end not in idx or len(dates) < 2:
        return None
    window = dates[-width:] if len(dates) >= width else dates
    if window[-1] != end:
        return None
    best = None
    for start_day in window:
        candidate = _window_from(by_day, idx, end, start_day, limit, width)
        if candidate is None:
            continue
        score = (candidate['gain_pct'] - candidate['index_gain_pct'], -candidate['days'])
        if best is None or score > best[0]:
            best = (score, candidate)
    return best[1] if best else None


def _index_prev(idx, day):
    ordered = sorted(idx)
    if day not in idx:
        return None
    i = ordered.index(day)
    if i == 0:
        return None
    prev = idx[ordered[i - 1]]
    return float(prev.get('close') or 0)


def _window_from(by_day, idx, end, start_day, limit, width):
    stock_end = float(by_day[end]['close'])
    stock_start = float(by_day[start_day]['prev_close'] or 0)
    if stock_start <= 0 or stock_end <= 0 or start_day not in idx or end not in idx:
        return None
    index_end = float(idx[end]['close'])
    index_start = _index_prev(idx, start_day)
    if not index_start or index_start <= 0 or index_end <= 0:
        return None
    ordered = sorted(d for d in by_day if start_day <= d <= end)
    days = len(ordered)
    # 交易所口径：10/30 个交易日窗口用含首尾的天数，最多 9/29 段涨跌。
    if days <= 0 or days > width - 1:
        return None
    next_dates = [d for d in sorted(idx) if d >= start_day][:3]
    start2 = next_dates[1] if len(next_dates) > 1 else start_day
    start3 = next_dates[2] if len(next_dates) > 2 else start2
    stock_start2 = float((by_day.get(start2) or {}).get('prev_close') or stock_start)
    stock_start3 = float((by_day.get(start3) or {}).get('prev_close') or stock_start2)
    index_start2 = _index_prev(idx, start2) or index_start
    index_start3 = _index_prev(idx, start3) or index_start2
    gain = _pct(stock_end, stock_start)
    index_gain = _pct(index_end, index_start)
    space = round(limit - (gain - index_gain), 2)
    price = _threshold_price(stock_start, index_end, index_start, limit)
    price2 = _threshold_price(stock_start2, index_end, index_start2, limit)
    price3 = _threshold_price(stock_start3, index_end, index_start3, limit)
    return {
        'days': days,
        'start_date': start_day,
        'gain_pct': gain,
        'index_gain_pct': index_gain,
        'index_last_px': index_end,
        'index_start_pre_close': index_start,
        'index_start_pre_close2': index_start2,
        'index_start_pre_close3': index_start3,
        'space_pct': space,
        'start_pre_close': stock_start,
        'start_pre_close2': stock_start2,
        'start_pre_close3': stock_start3,
        'threshold_gain_pct': round(_pct(price, stock_end), 2),
        'threshold_gain_pct2': round(_pct(price2, stock_end), 2),
        'threshold_gain_pct3': round(_pct(price3, stock_end), 2),
        'threshold_price': price,
        'threshold_price2': price2,
        'threshold_price3': price3,
    }


def _eligible(item: dict) -> bool:
    t1, t2 = item.get('t1') or {}, item.get('t2') or {}
    spaces = [v for v in (t1.get('space_pct'), t2.get('space_pct')) if v is not None]
    if not spaces:
        return False
    nearest = min(spaces)
    if item.get('is_monitored'):
        return True
    return nearest <= NEAR_PCT


def compute(day: str, closes: list[dict], indexes: dict, notices: list[dict]) -> list[dict]:
    date = iso(day)
    by_code: dict[str, list[dict]] = {}
    names = {}
    for row in closes:
        code = normalize_code(row['stock_code'])
        by_code.setdefault(code, []).append(row)
        names[code] = row.get('stock_name') or names.get(code) or code
    idx_bars = {code: series.get('bars') or [] for code, series in (indexes.get('series') or {}).items()}
    notice_map = {}
    for item in notices:
        code = normalize_code(item['stock_code'])
        notice_map.setdefault(code, []).append(item)
    out = []
    for code, rows in by_code.items():
        board = board_of(code, names.get(code, ''))
        if not board:
            continue
        index_code = INDEX_FOR_BOARD[board]
        bars = idx_bars.get(index_code) or []
        if not bars:
            continue
        t1 = _best_window(rows, bars, date, T1_WINDOW, T1_LIMIT)
        t2 = _best_window(rows, bars, date, T2_WINDOW, T2_LIMIT)
        if not t1 and not t2:
            continue
        close_row = next((r for r in rows if r['trade_date'] == date), None)
        if not close_row:
            continue
        calendar = sorted({r['trade_date'] for r in rows} | {b['trade_date'] for b in bars} | {date})
        windows = sorted(notice_map.get(code) or [], key=lambda n: n['notice_date'])
        active = expired = None
        for n in reversed(windows):
            start = n['notice_date']
            if start > date:
                continue
            monitor_end = _plus_trade_days(start, 9, calendar)
            if start <= date <= monitor_end:
                active = n
                break
            if expired is None and monitor_end < date:
                expired = {**n, 'monitor_end_date': monitor_end}
                recent = [d for d in calendar if monitor_end < d <= date]
                if len(recent) > 5:
                    expired = None
        item = {
            'symbol_code': code,
            'symbol_name': names.get(code) or code,
            'date1': date,
            'end_close': float(close_row['close']),
            'last_px': float(close_row['close']),
            'px_change_rate': round((float(close_row['close']) / float(close_row['prev_close']) - 1) * 100, 2) if close_row.get('prev_close') else None,
            'board': board,
            'index_code': index_code,
            'is_monitored': bool(active),
            'monitor_expired': bool(expired) and not active,
            'monitor_start_date': (active or expired or {}).get('notice_date'),
            'monitor_end_date': (expired or {}).get('monitor_end_date') or (_plus_trade_days((active or {}).get('notice_date'), 9, calendar) if active else None),
            't1': t1,
            't2': t2,
        }
        if _eligible(item):
            out.append(_flatten(item))
    out.sort(key=lambda r: (
        0 if r.get('is_monitored') else 1,
        min([v for v in (r.get('t1_space_pct'), r.get('t2_space_pct')) if v is not None] or [10**9]),
        -(r.get('px_change_rate') or 0),
        r['symbol_code'],
    ))
    for i, row in enumerate(out, 1):
        row['rank'] = i
    return out


def _plus_calendar_days(day: str | None, n: int) -> str | None:
    if not day:
        return None
    return (datetime.fromisoformat(iso(day)).date() + timedelta(days=n)).isoformat()


def _minus_days(day: str, n: int) -> str:
    return (datetime.fromisoformat(iso(day)).date() - timedelta(days=n)).isoformat()


def _plus_trade_days(start: str, n: int, calendar: list[str]) -> str:
    days = sorted({iso(d) for d in calendar if iso(d) >= iso(start)})
    if not days:
        return iso(start)
    return days[min(n, len(days) - 1)]


def _flatten(item: dict) -> dict:
    row = {k: v for k, v in item.items() if k not in ('t1', 't2')}
    for prefix, block in (('t1', item.get('t1') or {}), ('t2', item.get('t2') or {})):
        mapping = {
            'days': f'{prefix}_days',
            'gain_pct': f'{prefix}_gain_pct',
            'index_gain_pct': f'{prefix}_index_gain_pct',
            'index_last_px': f'{prefix}_index_last_px',
            'index_start_pre_close': f'{prefix}_index_start_pre_close',
            'index_start_pre_close2': f'{prefix}_index_start_pre_close2',
            'index_start_pre_close3': f'{prefix}_index_start_pre_close3',
            'space_pct': f'{prefix}_space_pct',
            'start_date': f'{prefix}_start_date',
            'start_pre_close': f'{prefix}_start_pre_close',
            'start_pre_close2': f'{prefix}_start_pre_close2',
            'start_pre_close3': f'{prefix}_start_pre_close3',
            'threshold_gain_pct': f'{prefix}_threshold_gain_pct',
            'threshold_gain_pct2': f'{prefix}_threshold_gain_pct2',
            'threshold_gain_pct3': f'{prefix}_threshold_gain_pct3',
            'threshold_price': f'{prefix}_threshold_price',
            'threshold_price2': f'{prefix}_threshold_price2',
            'threshold_price3': f'{prefix}_threshold_price3',
        }
        for src, dest in mapping.items():
            row[dest] = block.get(src)
    return row


async def collect(day: str) -> dict:
    from app.datasources import cninfo
    date = iso(day)
    close_meta = ensure_close_ready(date)
    closes = store.daily_close_range(date, LOOKBACK_DAYS)
    dates = sorted({r['trade_date'] for r in closes})
    if date not in dates or len(dates) < T2_WINDOW:
        raise ValueError('Insufficient daily close coverage for 30-day abnormal move')
    # 收盘派生：缺指数快照时当场补齐，不依赖并行 job 抢跑成功
    indexes = index_history.published(date)
    if not indexes or indexes.get('complete') is not True:
        indexes = await index_history.collect(date)
    notices = await cninfo.severe_notices(_minus_days(date, 45), date)
    items = compute(date, closes, indexes, notices)
    payload = {
        'date': date,
        'complete': True,
        'total': len(items),
        'items': items,
        'source': 'daily_close+tencent_index+cninfo',
        'index_source': indexes.get('source'),
        'notice_count': len(notices),
        'close_coverage': close_meta.get('priced_count') or close_meta.get('count'),
        'close_meta_source': close_meta.get('source'),
        'formula': {
            't1': '10 trading-day stock return minus matching A-share index return, threshold 100%',
            't2': '30 trading-day stock return minus matching A-share index return, threshold 200%',
            'indexes': INDEX_FOR_BOARD,
            'monitor': 'cninfo 严重异常波动 notices; 10 trading-day focus window after notice date',
        },
        'collected_at': datetime.now(TZ).isoformat(),
        'empty_ok': True,
    }
    store.movement_save(date, payload)
    return payload


def ensure_close_ready(day: str) -> dict:
    """异动等收盘派生产品的日线就绪检查。

    优先用 daily_close_runs.complete；若只有 daily_close 行（成分补行情等路径）
    且计价行足够，则落一条 reconstructed run，避免读路径长期 missing。
    """
    import json
    date = iso(day)
    meta = store.daily_close_run(date)
    if meta and meta.get('complete') is True:
        return meta
    with store._conn() as conn:
        priced = conn.execute(
            "SELECT COUNT(*) AS n FROM daily_close "
            "WHERE trade_date=? AND close IS NOT NULL AND close>0",
            (date,),
        ).fetchone()["n"]
    if int(priced) < 1000:
        raise ValueError('Daily close snapshot required before movement alerts')
    meta = {
        'complete': True,
        'trade_date': date,
        'priced_count': int(priced),
        'source': 'reconstructed_from_daily_close_rows',
        'note': 'for close-derived products; EM complete run meta was missing',
    }
    with store._conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO daily_close_runs VALUES(?,?)",
            (date, json.dumps(meta, ensure_ascii=False)),
        )
    return meta


def published(day: str) -> dict | None:
    return store.movement_get(iso(day))


async def review(day: str | None = None, limit: int = 200) -> dict:
    date = iso(day) if day else None
    snap = published(date) if date else None
    if snap is None and date is None:
        latest = store.movement_latest()
        snap = latest
    if snap:
        items = list(snap.get('items') or [])[: max(1, min(limit, 500))]
        return {**snap, 'items': items, 'status': 'ok', 'returned': len(items)}
    if settings.collector_mode == 'embedded' and date:
        snap = await collect(date)
        items = list(snap.get('items') or [])[: max(1, min(limit, 500))]
        return {**snap, 'items': items, 'status': 'ok', 'returned': len(items)}
    return {'date': date, 'items': [], 'total': 0, 'status': 'missing', 'source': 'published_movement'}
