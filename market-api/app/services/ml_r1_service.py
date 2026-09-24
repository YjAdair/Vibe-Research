"""只读已准备的ML-R1快照；每次批量查询共用缓存，不回源、不补数。"""
from __future__ import annotations
from app.core.store import store
from app.services.ml_r1 import compound, mean, total, number, pct_bucket, VERSION, SOURCE_ALLOWLIST, WEIGHTS

TAXONOMY_VERSION = 'eastmoney_native_v1'
def taxonomy_for(plate_type):
    return 'target_ml_v1' if plate_type in (17, 18) else TAXONOMY_VERSION


PUBLISHABLE = {'final', 'partial_preview'}
REASONS = {'missing_target_catalog_snapshot': '缺少该日经过复核的分类与成员证据', 'target_catalog_snapshot_mismatch': '计算输入与已固定的分类成员快照不一致', 'price_coverage_below_95_percent': '有效价格样本不足5个或覆盖低于95%', 'missing_limit_diffusion': '缺少涨跌停制度或收盘状态', 'missing_verified_eod_time': '缺少经验证的盘后行情时点', 'missing_persisted_snapshot': '该日尚无ML-R1输入快照',
           'incomplete_window_final_values': '周期内缺少完整日值，预览值不参与多日正式聚合',
           'taxonomy_level_unverified': '分类层级与标准化截面尚未验证',
           'taxonomy_version_unverified': '该分类版本或历史成员尚未验证',
           'missing_trading_calendar': '缺少完整交易日历', 'non_trading_date': '选择日期不是已发布交易日',
           'missing_window_inputs': '周期内存在缺失输入，未跳过缺失日期',
           'missing_metric_inputs': '该指标尚缺必要输入', 'missing_member_return_series': '缺少连续的成员总收益序列',
           'source_not_allowlisted_or_unproven': '输入来源缺少独立证据',
           'formula_version_mismatch': '快照公式版本不匹配', 'missing_point_in_time_membership': '缺少当时已知的有效成员',
           'missing_market_universe': '缺少历史全市场基准成员', 'missing_20_day_volume': '缺少20日前置成交额',
           'missing_20_day_calendar': '缺少20日前置交易日', 'incomplete_taxonomy_catalog': '完整分类目录未验证',
           'cross_section_below_20_topics': '有效题材不足20个', 'cross_section_below_80_percent': '有效题材覆盖不足80%',
           'missing_security_master': '缺少历史上市日和证券状态', 'missing_corporate_actions': '缺少已验证公司行动',
           'missing_limit_regime': '缺少日期对应涨跌停制度', 'missing_flow_definition': '缺少统一口径资金明细'}


def reason_text(reasons):
    return '；'.join(('全市场：' + REASONS.get(r[7:], r[7:])) if r.startswith('market_') else REASONS.get(r, r) for r in reasons)


def missing(date, reasons, **extra):
    return {'date': date, 'formula_id': 'ml_r1', 'formula_version': VERSION, 'status': 'missing_input',
            'rows': [], 'coverage': {}, 'reasons': reasons, 'reason': reason_text(reasons), **extra}


def calendar_days():
    cal = store.kv_get('collector_calendar_v1', {}) or {}
    if cal.get('complete') is False or not (cal.get('verified_at') or cal.get('complete') is True):
        return []
    def iso(d):
        d = str(d).replace('-', '')
        return f'{d[:4]}-{d[4:6]}-{d[6:8]}'
    return sorted({iso(d) for d in cal.get('days', [])})


def get_day(plate_type, date, taxonomy_version=None, cache=None):
    version = taxonomy_version or taxonomy_for(plate_type)
    if plate_type not in (14, 15, 17, 18) or version != taxonomy_for(plate_type):
        return missing(date, ['taxonomy_version_unverified'], taxonomy_version=version)
    key = (plate_type, date, version)
    if cache is not None and key in cache:
        return cache[key]
    payload = store.ml_r1_snapshot_get(version, date, plate_type=plate_type)
    if not payload:
        from app.services import ml_catalog
        payload = missing(date, ['missing_persisted_snapshot'] + (['missing_target_catalog_snapshot'] if plate_type in (17, 18) and not ml_catalog.get(date) else []))
    elif payload.get('source') not in SOURCE_ALLOWLIST or not payload.get('source_evidence'):
        payload = missing(date, ['source_not_allowlisted_or_unproven'])
    elif payload.get('formula_version') != VERSION or payload.get('date') != date:
        payload = missing(date, ['formula_version_mismatch'])
    if plate_type in (17, 18) and payload.get('rows'):
        from app.services import ml_catalog
        if not payload.get('catalog_snapshot_id') or not ml_catalog.get(date, payload['catalog_snapshot_id']):
            payload = missing(date, ['missing_target_catalog_snapshot'])
    if plate_type == 18 and payload.get('rows'):
        parent = store.ml_r1_snapshot_get(version, date, plate_type=17)
        if parent and parent.get('catalog_snapshot_id') != payload.get('catalog_snapshot_id'):
            payload = missing(date, ['target_catalog_snapshot_mismatch'])
    payload = {**payload, 'taxonomy_version': version, 'formula_id': 'ml_r1'}
    if cache is not None:
        cache[key] = payload
    return payload


def _window(date, count, calendar):
    if date not in calendar:
        return []
    index = calendar.index(date)
    return calendar[max(0, index - count + 1):index + 1]


def rank(plate_type, date, n_days, n_type, limit, taxonomy_version=None, cache=None, calendar=None):
    if n_days not in (1, 3, 5) or n_type not in (9, 1, 3):
        raise ValueError('ML-R1仅支持1/3/5日与强度/涨幅/资金排序')
    version = taxonomy_version or taxonomy_for(plate_type)
    if plate_type not in (14, 15, 17, 18) or version != taxonomy_for(plate_type):
        return missing(date, ['taxonomy_version_unverified'])
    calendar = calendar if calendar is not None else calendar_days()
    selected = _window(date, n_days, calendar)
    if len(selected) != n_days:
        return missing(date, ['missing_trading_calendar' if not calendar else 'non_trading_date' if date not in calendar else 'missing_window_inputs'])
    snaps = [get_day(plate_type, d, version, cache) for d in selected]
    if any(not s.get('rows') for s in snaps):
        return missing(date, list(dict.fromkeys(r for s in snaps for r in s.get('reasons', []))) or ['missing_window_inputs'])
    maps = [{r['plate_code']: r for r in s['rows']} for s in snaps]
    codes = set.intersection(*(set(m) for m in maps))
    field, metric = {9: ('sum_score', 'strength'), 1: ('sum_rate', 'return'), 3: ('sum_leader_money', 'money')}[n_type]
    out = []
    for code in codes:
        rows = [m[code] for m in maps]
        allowed = {'final'} if n_days > 1 else PUBLISHABLE
        if any(r.get('metric_status', {}).get(metric) not in allowed for r in rows):
            continue
        scores, returns, flows = ([r.get('strength') for r in rows], [r.get('return') for r in rows], [r.get('leader_money') for r in rows])
        score, rate = mean(scores), compound(returns)
        definitions = {r.get('flow_definition') for r in rows}
        money = total(flows) if len(definitions) == 1 and None not in definitions else None
        vals = {'sum_score': score, 'sum_rate': rate * 100 if rate is not None else None, 'sum_leader_money': money}
        if vals[field] is None:
            continue
        status = 'final' if all(r['metric_status'][metric] == 'final' for r in rows) else 'partial_preview'
        latest = rows[-1]
        contributions = {k: mean([(r.get('contributions') or {}).get(k) for r in rows]) for k in WEIGHTS}
        out.append({'plate_code': code, 'plate_name': latest['plate_name'], **vals, 'status': status,
                    'formula_id': 'ml_r1', 'formula_version': VERSION, 'taxonomy_version': version, 'days': n_days,
                    'contributions': contributions, 'factors': [{'date': d, 'factors': r.get('factors', {})} for d, r in zip(selected, rows)],
                    'coverage': {'daily': [{'date': d, **r.get('coverage', {})} for d, r in zip(selected, rows)]},
                    'source': snaps[-1].get('source'), 'source_as_of': snaps[-1].get('source_as_of'),
                    'catalog_snapshot_ids': [s.get('catalog_snapshot_id') for s in snaps],
                    'collected_at': snaps[-1].get('collected_at'), 'input_snapshot_ids': [s.get('snapshot_id') for s in snaps],
                    'reasons': [] if status == 'final' else ['partial_preview']})
    if not out:
        reasons = list(dict.fromkeys(r for s in snaps for row in s['rows'] for r in row.get('reasons', [])))
        return missing(date, reasons or ['incomplete_window_final_values' if n_days > 1 else 'missing_metric_inputs'])
    return sorted(out, key=lambda r: (-r[field], r['plate_code']))[:limit]


def rank_batch(plate_type, dates, n_days, n_type, limit, taxonomy_version):
    cache, calendar = {}, calendar_days()
    columns = []
    for date in dates:
        data = rank(plate_type, date, n_days, n_type, limit, taxonomy_version, cache, calendar)
        if isinstance(data, dict):
            columns.append(data)
        else:
            columns.append({'date': date, 'status': 'partial_preview' if any(r['status'] == 'partial_preview' for r in data) else 'available', 'rows': data})
    status = 'available' if columns and all(c['status'] == 'available' for c in columns) else 'partial_preview' if any(c.get('rows') for c in columns) else 'missing_input'
    return {'columns': columns, 'status': status, 'meta': {'formula_id': 'ml_r1', 'formula_version': VERSION,
            'taxonomy_version': taxonomy_version or taxonomy_for(plate_type), 'n_days': n_days, 'n_type': n_type}}


def trend(plate_type, code, start, end, taxonomy_version=None):
    rows = []
    native = {}
    if plate_type in (14, 15) and (taxonomy_version or TAXONOMY_VERSION) == TAXONOMY_VERSION:
        source_rows, source_runs = store.board_snapshot_range(2 if plate_type == 14 else 3, start, end)
        native = {r['date1']: r for r in source_rows if r.get('plate_code') == code and r.get('source') == 'eastmoney'}
    for date in calendar_days():
        if not start <= date <= end:
            continue
        snap = get_day(plate_type, date, taxonomy_version)
        row = next((r for r in snap.get('rows', []) if r['plate_code'] == code), {})
        raw = native.get(date, {})
        amount = row.get('trade_money') if row.get('trade_money') is not None else raw.get('trade_money')
        rows.append({'date1': date, 'score': row.get('strength'), **{k: row.get(k) for k in ('trade_money', 'money_leader', 'money_leader_buy', 'money_leader_sell')},
                     'formula_version': VERSION, 'input_snapshot_id': snap.get('snapshot_id'), 'catalog_snapshot_id': snap.get('catalog_snapshot_id'),
                     'metric_status': row.get('metric_status', {}), 'collected_at': snap.get('collected_at'),
                     'status': row.get('status', 'missing_input'), 'coverage': row.get('coverage', {}),
                     'reasons': row.get('reasons', snap.get('reasons', [])), 'source_as_of': snap.get('source_as_of'), 'source': snap.get('source'),
                     'trade_money': amount, 'amount_source': 'ml_r1_members' if row.get('trade_money') is not None else 'eastmoney_native' if raw else None,
                     'amount_source_timestamp': raw.get('source_timestamp')})
    return {'rows': rows, 'amount_basis': '成员成交额优先；缺失时单独显示东财原生成交额（非自研评分因子）', 'plate_code': code, 'status': 'available' if any(r['score'] is not None or r['trade_money'] is not None for r in rows) else 'missing_input',
            'formula_id': 'ml_r1', 'formula_version': VERSION, 'taxonomy_version': taxonomy_version or taxonomy_for(plate_type)}


def pct(plate_type, code, dates, days, taxonomy_version=None):
    if days not in (5, 10, 20):
        raise ValueError('ML-R1涨幅仅支持5/10/20日')
    cache, calendar, result = {}, calendar_days(), {}
    for date in dates:
        window = _window(date, days, calendar)
        rows = []
        for d in window:
            snap = get_day(plate_type, d, taxonomy_version, cache)
            rows.append(next((r for r in snap.get('rows', []) if r['plate_code'] == code), {}))
        ready = len(window) == days and all(r.get('member_returns') and r.get('membership_as_of')
                    and r.get('metric_status', {}).get('return') == 'final' for r in rows)
        buckets = {k: [] for k in ('100+', '80-100', '60-80', '40-60', '20-40')}
        valid, expected = 0, rows[-1].get('coverage', {}).get('eligible', len(rows[-1].get('member_returns', {}))) if rows else 0
        ranked = []
        if ready:
            # 必须在整个窗口具有当时有效成员与总收益，不将期末成员倒灌历史。
            for stock, info in rows[-1]['member_returns'].items():
                values = [(r.get('member_returns', {}).get(stock) or {}).get('return') for r in rows]
                value = compound(values)
                if value is None:
                    continue
                valid += 1
                ranked.append({'stock_code': stock, 'stock_name': info['stock_name'], 'sum_rate': value * 100})
                bucket = pct_bucket(value * 100)
                if bucket:
                    buckets[bucket].append({'stock_code': stock, 'stock_name': info['stock_name'], 'cum_pct': value * 100})
        for items in buckets.values():
            items.sort(key=lambda r: (-r['cum_pct'], r['stock_code']))
        status = 'final' if ready and valid == expected and expected else 'partial_preview' if ready and expected and valid / expected >= .95 else 'missing_input'
        if status == 'missing_input':
            buckets = {}
        meta = {'status': status, 'reasons': [] if status == 'final' else ['缺少连续的成员总收益序列'],
                'coverage': {'valid': valid, 'expected': expected}, 'taxonomy_version': taxonomy_version or taxonomy_for(plate_type),
                'input_snapshot_ids': [get_day(plate_type, d, taxonomy_version, cache).get('snapshot_id') for d in window], 'formula_id': 'ml_r1', 'formula_version': VERSION}
        ranked.sort(key=lambda r: (-r['sum_rate'], r['stock_code']))
        for rank_number, item in enumerate(ranked, 1):
            item['rank'] = rank_number
        result[date] = {'stocks': buckets, 'items': ranked if status != 'missing_input' else [],
                        'meta': meta, 'status': status, 'days': days}
    return result


def daily_nav(plate_type: int, unique_key: str, date1: str = "", n: int = 30) -> dict:
    """Return a daily NAV line only from validated, point-in-time ML-R1 rows."""
    calendar = [d for d in calendar_days() if not date1 or d <= date1]
    end = date1 if date1 else (calendar[-1] if calendar else None)
    dates = [d for d in calendar if d <= end][-max(1, int(n or 30)):] if end else []
    if not end or len(dates) != max(1, int(n or 30)):
        return {"status": "missing", "reason": "missing_continuous_ml_r1_returns", "x": [], "y": [], "vol": [], "stocks": []}
    nav = 1000.0
    x, y, coverage, stocks = [], [], [], []
    for date in dates:
        snap = get_day(plate_type, date)
        row = next((r for r in snap.get("rows", []) if str(r.get("plate_code")) == unique_key), None)
        returns = (row or {}).get("member_returns")
        valid = (row or {}).get("metric_status", {}).get("return") == "final"
        if not valid or not returns or not (row or {}).get("membership_as_of") or (row or {}).get("return") is None:
            return {"status": "missing", "reason": "missing_continuous_ml_r1_returns", "x": [], "y": [], "vol": [], "stocks": []}
        nav *= 1 + float(row["return"])
        x.append(date.replace("-", ""))
        y.append(round(nav, 4))
        coverage.append({"date": date, "members": len(returns), "membership_as_of": row["membership_as_of"]})
        if date == dates[-1]:
            stocks = [{"code": code, "name": info.get("stock_name", code)} for code, info in returns.items()]
    return {"status": "ok", "x": x, "y": y, "vol": [], "stocks": stocks, "coverage": coverage,
            "series_kind": "daily_nav", "source": "ml_r1_prepared_daily_returns"}

