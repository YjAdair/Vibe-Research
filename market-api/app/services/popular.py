"""同花顺热股人气榜：采集发布后只读。"""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import settings
from app.core.store import store
from app.datasources import ths
from app.datasources.codes import normalize_code

TZ = ZoneInfo('Asia/Shanghai')
INDEPENDENT_SOURCES = {'10jqka_eq_hot_list', 'ths', 'eastmoney', 'eastmoney_public'}
# 用户 2026-09-22 授权：缺免费深表/历史时，源站 ths/top 存档可写入产品人气快照（过渡）。
ORIGIN_BRIDGE_SOURCES = {'zizizaizai_ths_top_calibration', 'origin_ths_top'}
PUBLISHED_SOURCES = INDEPENDENT_SOURCES | ORIGIN_BRIDGE_SOURCES
DEEP_MIN_TOTAL = 1000
ORIGIN_THS_TOP_URL = 'https://api.zizizaizai.com/v3/open/sentiment/media/ths/top'
ORIGIN_THS_TOP_SOURCE = 'zizizaizai_ths_top_calibration'


async def collect_origin_ths_top(day: str, *, force: bool = False) -> dict:
    """用户授权过渡：原站 ths/top 深表写入 popular_snapshots。

    免费 Top100 不能覆盖已落库深表；本函数在缺深表或 force 时写入。
    """
    import httpx
    from app.config import settings

    date = iso(day)
    if not settings.enable_origin_reference and not force:
        return {'date': date, 'skipped': 'origin_reference_disabled', 'total': 0}
    existing = store.popular_get(date)
    if (not force and existing and (existing.get('total') or 0) >= DEEP_MIN_TOTAL
            and existing.get('source') in ORIGIN_BRIDGE_SOURCES):
        return {'date': date, 'skipped': 'already_deep', 'total': existing.get('total') or 0,
                'source': existing.get('source')}
    async with httpx.AsyncClient(
        timeout=30,
        headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://quant.zizizaizai.com/'},
    ) as client:
        resp = await client.get(ORIGIN_THS_TOP_URL, params={'date1': date, 'top_n': 20000})
        resp.raise_for_status()
        body = resp.json()
    if body.get('code') != 20000:
        raise ValueError(f"ths/top rejected: {body.get('code')}")
    rows = body.get('data') or []
    if not rows:
        return {'date': date, 'skipped': 'empty', 'total': 0}
    items = []
    for r in sorted(rows, key=lambda x: (x.get('rank') or 0, x.get('symbol_code') or '')):
        code = r.get('symbol_code')
        if not code:
            continue
        items.append({
            'symbol_code': normalize_code(code),
            'symbol_name': r.get('symbol_name') or '',
            'rank': r.get('rank'),
            'src_rank': r.get('rank'),
            'rank_diff': r.get('rank_diff'),
            'heat': r.get('attention'),
            'attention': r.get('attention'),
            'market': None,
            'concept_tag': [],
        })
    if len(items) < DEEP_MIN_TOTAL:
        raise ValueError(f'incomplete ths/top deep list: {len(items)}')
    payload = {
        'date': date,
        'complete': True,
        'total': len(items),
        'items': items,
        'source': ORIGIN_THS_TOP_SOURCE,
        'kind': 'day',
        'quote_coverage': 0,
        'history_note': 'origin ths/top daily archive; user-authorized bridge until free deep hist exists',
        'collected_at': datetime.now(TZ).isoformat(),
    }
    store.popular_save(date, payload)
    return {'date': date, 'total': len(items), 'source': ORIGIN_THS_TOP_SOURCE}


def iso(day: str) -> str:
    d = day.replace('-', '')
    if len(d) != 8 or not d.isdigit():
        raise ValueError('Invalid trade date')
    return f'{d[:4]}-{d[4:6]}-{d[6:]}'


def enrich(items: list[dict], closes: dict[str, dict]) -> list[dict]:
    out = []
    for row in items:
        code = normalize_code(row['symbol_code'])
        close = closes.get(code) or {}
        last = close.get('close')
        prev = close.get('prev_close')
        pct = None
        if last and prev:
            pct = round((float(last) / float(prev) - 1) * 100, 2)
        out.append({
            **row,
            'symbol_code': code,
            'symbol_name': row.get('symbol_name') or close.get('stock_name') or code,
            'last_price': last,
            'last_pct': pct,
            'circulation_value': close.get('circulation_value'),
            'turnover_ratio': close.get('turnover_ratio'),
            'vol_ratio': close.get('vol_ratio'),
            'open_px': close.get('open'),
            'high_px': close.get('high'),
            'industry': close.get('market_type') or row.get('industry'),
            'quote_source': 'daily_close' if last else None,
        })
    return out


async def collect(day: str, minute: str | None = None) -> dict:
    date = iso(day)
    raw = await ths.hot_stock_list('hour')
    items = raw['items']
    if raw['total'] != 100 or len(items) != 100:
        raise ValueError('ths hot list is not a complete top 100')
    closes = {r['stock_code']: r for r in store.daily_close_range(date, 1) if r['trade_date'] == date}
    payload = {
        'date': date,
        'complete': True,
        'total': len(items),
        'items': enrich(items, closes),
        'source': raw['source'],
        'kind': raw['kind'],
        'quote_coverage': sum(1 for i in enrich(items, closes) if i.get('last_price') is not None),
        'history_note': 'ths public hot list has no dated replay; snapshot is the list visible at collect time',
        'collected_at': datetime.now(TZ).isoformat(),
    }
    enriched = enrich(items, closes)
    try:
        from app.datasources.industry_map import em_tree, fetch_f10_industries, ths_industry_of
        tree = await em_tree()
        catalog = set(ths.plate_catalog().get('14', {}).keys())
        code_by_plate = {code: name for name, code in ths.plate_catalog().get('14', {}).items()}
        f10 = await fetch_f10_industries([row['symbol_code'] for row in enriched])
        for row in enriched:
            f10_code = f10.get(row['symbol_code'])
            ths_ind = code_by_plate.get(f10_code) if f10_code else None
            if not ths_ind:
                ths_ind = ths_industry_of(row['symbol_code'], tree.get(row['symbol_code']), catalog)
            if ths_ind:
                row['ths_industry'] = ths_ind
    except Exception:
        pass
    payload['items'] = enriched
    payload['quote_coverage'] = sum(1 for i in payload['items'] if i.get('last_price') is not None)
    # 勿用 Top100 覆盖已落库的源站深表（~5500）
    existing = store.popular_get(date)
    if existing and (existing.get('total') or 0) >= DEEP_MIN_TOTAL and payload['total'] < DEEP_MIN_TOTAL:
        if minute:
            store.popular_minute_save(date, minute, payload)
        return {**existing, 'shallow_collect_skipped': True, 'shallow_collected_at': payload['collected_at']}
    store.popular_save(date, payload)
    if minute:
        store.popular_minute_save(date, minute, payload)
    return payload


def published(day: str | None = None, *, allow_stale: bool = False) -> dict | None:
    hit = store.popular_get(iso(day)) if day else store.popular_latest()
    if hit is None and day and allow_stale:
        hit = store.popular_latest(iso(day))
    return hit if hit and hit.get('source') in PUBLISHED_SOURCES else None


async def em_hist_snapshot(codes: set[str] | list[str], day: str, *, concurrency: int = 6) -> dict | None:
    """路径 A 历史：东财 getHisList 按成分拼日榜。source=em_hist，rank=东财股吧全市场名次。

    不写入 popular_snapshots（该表要求 TopN 连续 1..N）；只缓存个股日序。
    """
    import asyncio
    from app.datasources import eastmoney

    date = iso(day)
    uniq = sorted({normalize_code(c) for c in codes if c})
    if not uniq:
        return None
    need = store.em_popular_rank_uncovered(uniq, date)
    if need:
        sem = asyncio.Semaphore(max(1, min(concurrency, 8)))
        fetched_at = datetime.now(TZ).isoformat()

        async def _one(code: str) -> None:
            async with sem:
                try:
                    rows = await eastmoney.popularity_rank_history(code, year_type='2')
                except Exception:
                    # ponytail: 单票失败不阻断整板；cover 不写，下次可重试
                    return
                if not rows:
                    # 空序也记 cover，避免同日反复打空接口
                    store.em_popular_rank_set_cover(code, date, date, fetched_at)
                    return
                store.em_popular_rank_save_rows(rows)
                store.em_popular_rank_set_cover(
                    code, rows[0]['trade_date'], rows[-1]['trade_date'], fetched_at)

        await asyncio.gather(*[_one(c) for c in need])

    ranks = store.em_popular_rank_get_many(date, uniq)
    if not ranks:
        return None
    items = []
    for code, rk in ranks.items():
        prev = store.em_popular_rank_prev(code, date)
        items.append({
            'symbol_code': code,
            'symbol_name': '',
            'rank': rk,
            # 名次变化 = 前日名次 - 今日名次；缺前日保持 None（缺失≠0）
            'rank_diff': (prev - rk) if prev is not None else None,
            'attention': None,
            'heat': None,
        })
    items.sort(key=lambda r: (r['rank'], r['symbol_code']))
    return {
        'date': date,
        'complete': False,
        'total': len(items),
        'items': items,
        'source': 'em_hist',
        'rank_diff_basis': 'previous_em_hist_day',
        'history_note': 'assembled from EM getHisList per member; eastmoney guba rank, not THS Top100',
        'collected_at': datetime.now(TZ).isoformat(),
    }


def _attach_close(items: list[dict], date: str | None) -> list[dict]:
    if not date:
        return items
    closes = {r['stock_code']: r for r in store.daily_close_range(date, 1) if r['trade_date'] == date}
    if not closes:
        return items
    return enrich(items, closes)


async def review(day: str | None = None, top_n: int = 100) -> dict:
    top_n = max(1, min(int(top_n or 100), 100))
    date = iso(day) if day else None
    snap = published(date, allow_stale=False) if date else published()
    if snap:
        items = _attach_close(list(snap.get('items') or []), snap.get('date') or date)[:top_n]
        stale = bool(date is None and snap.get('date') != datetime.now(TZ).date().isoformat())
        return {
            **snap,
            'items': items,
            'returned': len(items),
            'status': 'ok',
            'stale': stale,
            'fell_back': False,
        }
    if settings.collector_mode == 'embedded':
        snap = await collect(date or datetime.now(TZ).date().isoformat())
        items = list(snap.get('items') or [])[:top_n]
        return {**snap, 'items': items, 'returned': len(items), 'status': 'ok', 'stale': False, 'fell_back': False}
    return {'date': date, 'items': [], 'total': 0, 'returned': 0, 'status': 'missing', 'source': 'published_popular'}


KING_ROW_FIELDS = ('symbol_code', 'symbol_name', 'rank', 'rank_diff', 'px_change_rate',
                   'attention', 'circulation_value', 'high_change', 'open_change',
                   'turnover_ratio', 'vol_ratio')
KING_BAND0_FIELDS = ('attention', 'rank', 'rank_diff', 'symbol_code')


def _king_band(rows: list[dict] | None, lo: int, hi: int) -> list[dict]:
    out = []
    for row in rows or []:
        try:
            rank = int(row.get('rank') or 0)
        except (TypeError, ValueError):
            continue
        if lo <= rank <= hi:
            out.append(row)
    out.sort(key=lambda r: int(r.get('rank') or 0))
    return out


async def king_rank(day: str | None = None, rank_down: int = 1, rank_up: int = 100, is_real: int = 1) -> dict:
    """易筋经资金龙虎：原站 /market/stocks/king/rank，扁平行、rank 带过滤、is_real=0 精简为 4 字段。

    数据链：king_pool_snapshots（原站快照，11 字段与原站一致，rank 可上千）
    -> 已发布同花顺热股近似（仅覆盖 rank<=100，字段为本地增强超集）。
    """
    lo = max(1, int(rank_down or 1))
    hi = max(lo, min(int(rank_up or 100), 10000))
    band0 = is_real is not None and int(is_real) == 0
    date = iso(day) if day else None
    if date:
        rows = store.king_pool_get(date)
    else:
        hit = store.king_pool_latest()
        date, rows = hit if hit else (None, None)
    if rows:
        items = _king_band(rows, lo, hi)
        if band0:
            items = [{k: r.get(k) for k in KING_BAND0_FIELDS} for r in items]
        return {
            'date': date,
            'status': 'ok',
            'source': 'king_pool_snapshot',
            'rank_down': lo,
            'rank_up': hi,
            'total': len(rows),
            'items': items,
            'note': 'rows are original-site king pool snapshot rows, rank-band filtered; coverage limited to captured rank bands',
        }
    data = await review(day, min(hi, 100))
    items = []
    for row in data.get('items') or []:
        rank = int(row.get('rank') or 0)
        if rank < lo or rank > hi:
            continue
        last = row.get('last_price')
        prev = None
        if last and row.get('last_pct') is not None:
            try:
                prev = float(last) / (1 + float(row['last_pct']) / 100.0)
            except (TypeError, ValueError, ZeroDivisionError):
                prev = None
        def _chg(px):
            if prev in (None, 0) or px in (None, 0):
                return None
            return round((float(px) / float(prev) - 1) * 100, 2)
        item = {
            'symbol_code': row.get('symbol_code'),
            'symbol_name': row.get('symbol_name'),
            'rank': rank,
            'rank_diff': row.get('rank_diff'),
            'px_change_rate': row.get('last_pct'),
            'last_price': last,
            'circulation_value': row.get('circulation_value'),
            'attention': row.get('heat'),
            'high_change': _chg(row.get('high_px')),
            'open_change': _chg(row.get('open_px')),
            'turnover_ratio': row.get('turnover_ratio'),
            'vol_ratio': row.get('vol_ratio'),
            'popularity_tag': row.get('popularity_tag'),
            'concept_tag': row.get('concept_tag') or [],
        }
        items.append({k: item.get(k) for k in KING_BAND0_FIELDS} if band0 else item)
    return {
        'date': data.get('date'),
        'status': data.get('status'),
        'source': data.get('source') or 'published_popular',
        'rank_down': lo,
        'rank_up': hi,
        'total': data.get('total') or 0,
        'items': items,
        'history_note': data.get('history_note'),
        'quote_coverage': data.get('quote_coverage'),
        'note': 'king/rank approximated by the published ths hot list plus daily_close (rank<=100 only); attention uses ths heat, not eastmoney guba',
    }


def _hash_plate_code(name: str) -> str:
    import hashlib
    digest = hashlib.sha1(name.encode('utf-8')).hexdigest()[:6]
    return 'TH' + digest.upper()


STAT_TAGS = ('2026中报预增', '2026一季报预增', '2026年报预增', '预增', '预亏', '首亏', '预减', '扭亏')
PLATE_TYPES = {14: 'ths_industry', 15: 'concept_all', 17: 'industry', 25: 'concept_core'}


def _canonical_name(name: str, plate_type: int) -> str:
    raw = (name or '').strip()
    if not raw:
        return raw
    code = ths.plate_code_for(raw, plate_type)
    if not code:
        return raw
    catalog = ths.plate_catalog().get(str(plate_type), {})
    for alias, mapped in catalog.items():
        if mapped == code:
            return alias
    return raw


def _plate_code(name: str, plate_type: int = 25) -> str:
    return ths.plate_code_for(name, plate_type) or _hash_plate_code(name)


def _tag_allowed(name: str, plate_type: int) -> bool:
    if not name:
        return False
    mapped = bool(ths.plate_code_for(name, plate_type))
    statistical = any(s in name for s in STAT_TAGS)
    if plate_type == 25:
        return (not statistical) and (mapped or True)
    if plate_type == 15:
        return True
    if plate_type == 17:
        return mapped
    if plate_type == 14:
        return mapped
    return False


def _row_tags(row: dict, plate_type: int) -> list[str]:
    if plate_type == 14:
        ths_ind = row.get('ths_industry')
        if ths_ind and _tag_allowed(ths_ind, plate_type):
            return [ths_ind]
        return []
    tags = list(row.get('concept_tag') or [])
    if plate_type in (14, 17):
        extra = row.get('industry') or row.get('hybk')
        if extra:
            tags.append(extra)
    seen = []
    for tag in tags:
        name = _canonical_name(str(tag).strip(), plate_type)
        if not name or not _tag_allowed(name, plate_type):
            continue
        if plate_type in (25, 14) and any(s in name for s in STAT_TAGS):
            continue
        if name not in seen:
            seen.append(name)
    return seen


def _aggregate_plates(data: dict, plate_type: int, top_n: int) -> dict:
    if data.get('status') != 'ok':
        return {'date': data.get('date'), 'status': data.get('status'), 'items': [], 'total': 0, 'source': data.get('source'), 'plate_type': plate_type}
    counts = {}
    members = {}
    for row in data.get('items') or []:
        for name in _row_tags(row, plate_type):
            counts[name] = counts.get(name, 0) + 1
            members.setdefault(name, []).append({
                'symbol_code': row.get('symbol_code'),
                'symbol_name': row.get('symbol_name'),
                'rank': row.get('rank'),
                'px_change_rate': row.get('last_pct') or row.get('px_change_rate'),
            })
    items = []
    date = data.get('date')
    mapped = 0
    for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        code = _plate_code(name, plate_type)
        if not str(code).startswith('TH'):
            mapped += 1
        items.append({
            'plate_code': code,
            'plate_name': name,
            'c_num': n,
            'date1': date,
            'stocks': members.get(name) or [],
        })
    top_n = max(1, min(int(top_n or 12), 100))
    kind = PLATE_TYPES.get(plate_type, 'unknown')
    return {
        'date': date,
        'status': 'ok',
        'source': data.get('source'),
        'plate_type': plate_type,
        'taxonomy': kind,
        'total': len(items),
        'items': items[:top_n],
        'all_count': len(items),
        'mapped_count': mapped,
        'note': 'c_num counts published ths hot-list tags; plate_code uses observed 88/80 codes when the name is in the local catalog, otherwise a stable TH hash',
    }


_PT25_POPULAR_SEED: dict | None = None


def _pt25_popular_seed() -> dict:
    """pt25 板块人气矩阵种子（原站 2026-09-11 快照 87 行），DB 缺失时兜底。"""
    global _PT25_POPULAR_SEED
    if _PT25_POPULAR_SEED is None:
        try:
            fp = Path(__file__).resolve().parents[1] / 'datasources' / 'pt25_popular_seed.json'
            _PT25_POPULAR_SEED = json.loads(fp.read_text(encoding='utf-8'))
        except Exception:  # noqa: BLE001
            _PT25_POPULAR_SEED = {}
    return _PT25_POPULAR_SEED


def _plate_popular_snapshot(plate_type: int, date: str) -> tuple[list[dict] | None, str]:
    rows = store.plate_popular_get(plate_type, date)
    if rows is not None:
        return rows, 'plate_popular_snapshot'
    if plate_type == 25:
        seed = _pt25_popular_seed().get(date)
        if seed is not None:
            return seed, 'seed_pt25_popular'
    return None, ''


async def plate_popular(day: str | None = None, top_n: int = 12, plate_type: int = 25) -> dict:
    """易筋经板块人气矩阵：原站 /plates/{pt}/rank/popular。

    行 {c_num,date1,plate_code,plate_name}；原站仅含 c_num>=3 的板块、按 c_num 降序，
    返回全量不按 top_n 截断（c_num=成分∩同花顺人气top100）。
    数据链：plate_popular_snapshots（原站快照）-> pt25 种子 -> 已发布热股标签聚合近似。
    25=概念核心，15=概念全量，17=精选(801xxx)，14=THS行业(881xxx)。
    """
    if plate_type not in PLATE_TYPES:
        return {'date': iso(day) if day else None, 'status': 'unsupported', 'items': [], 'total': 0, 'plate_type': plate_type, 'note': 'supported plate_type: 14/15/17/25'}
    date = iso(day) if day else datetime.now(TZ).date().isoformat()
    rows, source = _plate_popular_snapshot(plate_type, date)
    if rows is not None:
        return {
            'date': date,
            'status': 'ok',
            'source': source,
            'plate_type': plate_type,
            'taxonomy': PLATE_TYPES[plate_type],
            'total': len(rows),
            'items': rows,
            'all_count': len(rows),
            'mapped_count': len(rows),
            'note': 'rows are original-site rank/popular snapshot rows (c_num>=3, desc); top_n not applied, matching the original',
        }
    data = await review(day, 100)
    return _aggregate_plates(data, plate_type, top_n)


async def plate_popular_matrix(end: str | None = None, days: int = 6, top_n: int = 12, plate_type: int = 25) -> dict:
    """易筋经多日板块人气矩阵。缺日保持 missing，不回退、不打上游。

    每日数据链：plate_popular_snapshots（原站 rank/popular 快照，c_num 口径）
    -> pt25 种子 -> 14/15/17 的 plate_rank_daily（score 口径）-> 热股标签聚合近似。
    """
    from app.services import market
    if plate_type not in PLATE_TYPES:
        return {'status': 'unsupported', 'columns': [], 'plate_type': plate_type}
    days = max(1, min(int(days or 6), 15))
    top_n = max(1, min(int(top_n or 12), 100))
    calendar = await market.trade_days(max(days + 8, 20))
    calendar = [f'{d[:4]}-{d[4:6]}-{d[6:]}' if len(d) == 8 else d for d in calendar]
    if end:
        end = iso(end)
        calendar = [d for d in calendar if d <= end]
    calendar = calendar[-days:]
    columns = []
    snapshot_days = set()
    if plate_type in (14, 15, 17):
        snapshot_days = set(store.plate_rank_dates(plate_type, limit=days + 8))
    for day in calendar:
        pop_rows, pop_src = _plate_popular_snapshot(plate_type, day)
        if pop_rows is not None:
            columns.append({
                'date': day,
                'status': 'ok',
                'items': pop_rows[:top_n],
                'all_count': len(pop_rows),
                'mapped_count': len(pop_rows),
                'source': pop_src,
            })
        elif day in snapshot_days:
            rows = store.plate_rank_range(plate_type, day, day)
            rows.sort(key=lambda r: float(r.get('score') or 0), reverse=True)
            columns.append({
                'date': day,
                'status': 'ok',
                'items': rows[:top_n],
                'all_count': len(rows),
                'mapped_count': len(rows),
                'source': 'plate_rank_daily',
            })
        else:
            data = await plate_popular(day, top_n, plate_type)
            columns.append({
                'date': day,
                'status': data.get('status') or 'missing',
                'items': data.get('items') or [],
                'all_count': data.get('all_count') or 0,
                'mapped_count': data.get('mapped_count') or 0,
                'source': data.get('source'),
            })
    ok_n = sum(1 for c in columns if c['status'] == 'ok')
    sources = sorted({c.get('source') for c in columns if c.get('source')})
    return {
        'date': calendar[-1] if calendar else end,
        'days': calendar,
        'plate_type': plate_type,
        'taxonomy': PLATE_TYPES[plate_type],
        'top_n': top_n,
        'columns': columns,
        'status': 'ok' if ok_n else 'missing',
        'available_days': ok_n,
        'source': ','.join(sources) if sources else 'published_popular',
        'note': 'columns prefer original rank/popular snapshots (c_num), then plate_rank_daily score snapshots (14/15/17), then published hot-list aggregation',
    }
