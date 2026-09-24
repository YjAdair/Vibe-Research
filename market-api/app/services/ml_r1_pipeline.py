"""ML-R1后台预计算与独立本地输入适配。无网络；原站旧表不在读取白名单。

v2 输入链路（全部点时证据，缺失诚实标注）：
- 全市场基准：kv ml_r1:universe:*（09:30前采集快照，含 f26 上市日）。
- 行情：stock_kline_daily（腾讯raw OHLCV + 同花顺amount 回填；EOD快照增量）。
- 除权：经验证公司行动生成 tr_factor；原始前收与官方除权参考价分开保存。
- 题材成员：kv free_hotspots:members:{BK}:*（09:30前点时门禁）。
"""
import bisect
import copy
import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from app.core.store import store
from app.services import ml_r1, ml_r1_service

_num = ml_r1.number


def _latest_universe(date):
    """date 09:30 前已知的最新全市场清单快照；无则 None（点时证据不可追溯）。"""
    index = store.kv_get('ml_r1:universe:index', []) or []
    cutoff = date + 'T09:30:00'
    for key in sorted(index, reverse=True):
        rec = store.kv_get(key)
        if rec and ml_r1.known_before(rec.get('collected_at'), cutoff):
            return rec
    return None


def _limit_rule(meta, date):
    rule = meta.get('limit_rule') or {}
    if not rule and meta.get('security_rule_evidence'):
        from app.services.ml_limit_rules import resolve
        rule = resolve(date, meta['security_rule_evidence'])['rule'] or {}
    if (rule.get('verified') is not True or not rule.get('source')
            or not rule.get('effective_from') or rule['effective_from'] > date
            or (rule.get('effective_to') and date >= rule['effective_to'])
            or not ml_r1.known_before(rule.get('known_at'), date + 'T09:30:00')):
        return None
    return rule


def _iso_list_date(list_date):
    s = str(list_date or '')
    if len(s) == 8 and s.isdigit():
        return s[:4] + '-' + s[4:6] + '-' + s[6:]
    return None


def _listed_days(iso_list, date, calendar):
    """截至 date 的已上市交易日数。早于日历起点视为长期上市(9999)；未知 None。"""
    if iso_list is None or not calendar:
        return None
    if iso_list < calendar[0]:
        return 9999
    if iso_list > date:
        return 0
    lo = bisect.bisect_left(calendar, iso_list)
    hi = bisect.bisect_right(calendar, date)
    return max(hi - lo, 0)


def _stock_row(code, name, iso_list, date, prior, calendar, kwin, cache):
    """缺行和缺成交额保持未知；仅接受有日期和来源的明确停牌证据。"""
    if code in cache:
        return cache[code]
    listed = _listed_days(iso_list, date, calendar)
    row = {'code': code, 'name': name or code, 'listed_days': listed,
           'amount_history': [], 'amount_history_dates': list(prior),
           'trade_date': None, 'close': None, 'open': None, 'high': None, 'low': None,
           'prev_close': None, 'amount': None, 'tradable': None, 'halted': None,
           'corporate_action': None, 'total_return_factor': None,
           'total_return_verified': False, 'total_return_date': None, 'quote_return': None,
           'limit_applicable': None, 'close_limit_up': None,
           'close_limit_down': None, 'broken_limit_up': None}
    hist = kwin.get(code) or {}
    ah = []
    for d in prior:
        r = hist.get(d)
        amt = _num(r.get('amount')) if r else None
        if amt is not None:
            ah.append(amt)
        elif r and (r.get('input_meta') or {}).get('security_state') == 'suspended' and (r.get('input_meta') or {}).get('state_verified') is True and (r.get('input_meta') or {}).get('state_date') == d and (r.get('input_meta') or {}).get('state_source'):
            ah.append(0.0)
        else:
            ah.append(None)
    row['amount_history'] = ah
    cur = hist.get(date)
    if cur is None:
        cache[code] = row
        return row
    evidence = cur.get('input_meta') or {}
    state_ok = evidence.get('state_verified') is True and evidence.get('state_date') == date and bool(evidence.get('state_source'))
    is_halted = True if state_ok and evidence.get('security_state') == 'suspended' else False if state_ok and evidence.get('security_state') == 'trading' else None
    quote_ok = evidence.get('eod_verified') is True and ml_r1.verified_close_time(evidence.get('source_as_of'), date)
    row.update(trade_date=date, halted=is_halted, tradable=(not is_halted) if is_halted is not None and (is_halted or quote_ok) else None,
               close=_num(cur.get('close')), open=_num(cur.get('open')),
               high=_num(cur.get('high')), low=_num(cur.get('low')),
               prev_close=_num(cur.get('prev_close')), amount=_num(cur.get('amount')))
    if quote_ok and evidence.get('official_reference_verified') is True and row['close'] is not None and row['prev_close'] and row['prev_close'] > 0:
        row['quote_return'] = row['close'] / row['prev_close'] - 1
    tr = _num(cur.get('tr_factor'))
    if tr is not None and evidence.get('return_basis') == 'total_return_v1_1':
        row['total_return_factor'] = tr
        row['total_return_verified'] = True
        row['total_return_date'] = date
    if cur.get('action_day'):
        row['corporate_action'] = {'kind': 'corporate_action',
                                   'verified': row['total_return_verified'], 'ex_date': date}
    else:
        # 无tr证据（如公司行动账本缺失的股票）时，'none'声明同样不可验证，
        # total_return 将诚实返回None，不用未验证的close/prev冒充收益。
        row['corporate_action'] = {'kind': 'none', 'verified': row['total_return_verified']}
    pc = row['prev_close']
    rule = _limit_rule(evidence, date)
    if rule and rule.get('applicable') is False:
        row['limit_applicable'] = False
    elif rule and rule.get('applicable') is True and pc is not None and pc > 0 and evidence.get('official_reference_verified') is True:
        rate = _num(rule.get('rate'))
        tick = str(rule.get('tick') or '')
        upper = ml_r1.limit_price(pc, rate, tick, minimum_move=rule.get('minimum_move') is True) if rate is not None and tick else None
        lower = ml_r1.limit_price(pc, -rate, tick, minimum_move=rule.get('minimum_move') is True) if rate is not None and tick else None
        if upper is not None and lower is not None:
            row['limit_applicable'] = True
            if quote_ok and is_halted is False:
                row.update(ml_r1.limit_state(row['close'], row['high'], upper, lower))
    cache[code] = row
    return row


def prepared_types(date):
    from app.services import ml_catalog
    return (14, 15, 17, 18) if ml_catalog.get(date) else (14, 15)


def native_catalog(plate_type):
    board_type = {14: 2, 15: 3}[plate_type]
    rows, _ = store.board_snapshot_range(board_type, '0001-01-01', '9999-12-31')
    seen = {}
    for r in rows:
        if r.get('source') == 'eastmoney' and r.get('plate_code') not in seen:
            seen[r['plate_code']] = {'plate_type': plate_type, 'plate_code': r['plate_code'], 'plate_name': r.get('plate_name', r['plate_code'])}
    return sorted(seen.values(), key=lambda r: (r['plate_name'], r['plate_code']))


def build_local_input(date, plate_type):
    calendar = ml_r1_service.calendar_days()
    prior = [d for d in calendar if d < date][-20:]
    gaps = []

    kwin = store.kline_window(prior[0], date) if prior else {}
    row_cache = {}
    flow_rows = {r['stock_code']: r for r in store.stock_flow_range(date, date)}
    flow_run = store.kv_get('stock_flow_run:' + date, {}) or {}

    univ = _latest_universe(date)
    meta = {s['code']: s for s in (univ or {}).get('stocks', [])}
    market = []
    if univ:
        for s in univ['stocks']:
            market.append(_stock_row(s['code'], s.get('name'), _iso_list_date(s.get('list_date')),
                                     date, prior, calendar, kwin, row_cache))
    else:
        gaps.append('missing_market_universe')

    target_record = None
    if plate_type in (17, 18):
        from app.services import ml_catalog
        parent_snap = store.ml_r1_snapshot_get(ml_catalog.VERSION, date, plate_type=17) if plate_type == 18 else None
        target_nodes, target_record = ml_catalog.nodes(date, plate_type, (parent_snap or {}).get('catalog_snapshot_id'))
        seen = {n['code']: n['name'] for n in target_nodes}
        catalog_complete = plate_type in (target_record or {}).get('complete_levels', [])
        if not target_record:
            gaps.append('missing_target_catalog_snapshot')
    else:
        # 当日完整板块快照目录（点时）；缺当日快照则目录不完整，不用历史目录冒充。
        board_type = {14: 2, 15: 3}[plate_type]
        snap_rows, snap_runs = store.board_snapshot_range(board_type, date, date)
        catalog_complete = any(bool(r.get('complete')) and r.get('trade_date') == date for r in snap_runs)
        level = store.kv_get('ml_r1:catalog_evidence:' + str(plate_type) + ':' + date, {}) or {}
        if not (level.get('verified') is True and level.get('source') and level.get('level')
                and ml_r1.known_before(level.get('known_at'), date + 'T09:30:00')):
            catalog_complete = False
            gaps.append('taxonomy_level_unverified')
        seen = {}
        for r in snap_rows:
            seen.setdefault(str(r.get('plate_code')), r.get('plate_name', ''))
    if not catalog_complete:
        gaps.append('incomplete_taxonomy_catalog')


    cutoff = date + 'T09:30:00'
    topics = []
    for code in sorted(seen):
        if plate_type in (17, 18):
            record = ml_catalog.membership(date, plate_type, code, target_record['catalog_snapshot_id'])
            if record['status'] not in ('ok','partial_membership'):
                record = {}
        else:
            # 只读取独立采集器的成员；不读取plate_members_snapshots等原站旧数据。
            index = store.kv_get('free_hotspots:members:' + code + ':index', []) or []
            prefix = 'free_hotspots:members:' + code + ':'
            records = [store.kv_get(d if d.startswith(prefix) else prefix + d) for d in index]
            records = [r for r in records if r and r.get('source') == 'eastmoney'
                       and r.get('status') == 'ok' and ml_r1.known_before(r.get('collected_at'), cutoff)]
            record = max(records, key=lambda r: r['collected_at']) if records else {}
        members, dedup = [], set()
        for stock in record.get('stocks', []):
            if stock in dedup:
                continue
            dedup.add(stock)
            m = meta.get(stock) or {}
            quote = record.get('quotes', {}).get(stock, {})
            members.append(_stock_row(stock, m.get('name') or quote.get('name'),
                                      _iso_list_date(m.get('list_date')),
                                      date, prior, calendar, kwin, row_cache))
        topics.append({'code': code, 'name': seen[code], 'members': members,
                       'membership_verified': bool(record) and record.get('status') != 'partial_membership',
                       'membership_known_at': record.get('known_at') or record.get('collected_at'),
                       'effective_from': date if record else None})

    for stock_code, row in row_cache.items():
        f = flow_rows.get(stock_code) or {}
        if (flow_run.get('source') == 'eastmoney_push2delay_f62'
                and ml_r1.verified_close_time(f.get('source_as_of'), date)):
            row.update(main_net_inflow=_num(f.get('main_net_inflow')), flow_verified=True,
                       flow_date=date, flow_definition='eastmoney_push2delay_f62')

    eod = store.kv_get('ml_r1:eod_quote_run:' + date)
    if not eod or not eod.get('source_as_of'):
        gaps.append('missing_verified_eod_time')

    return {'date': date, 'plate_type': plate_type,
            'taxonomy_version': ml_r1_service.taxonomy_for(plate_type),
            'catalog_snapshot_id': (target_record or {}).get('catalog_snapshot_id'),
            'source': 'local_independent',
            'source_evidence': ('universe=kv ml_r1:universe point-time; '
                                'kline=stock_kline_daily(tencent_ifzq_ths_v1 backfill + eastmoney_snapshot_eod); '
                                'corp_actions=sina_bonus backfill; '
                                'members=kv free_hotspots:members point-time; '
                                'catalog=validated target bundle or native same-day complete run'),
            'market': market, 'market_full_universe': bool(univ and univ.get('complete')),
            'market_known_at': (univ or {}).get('collected_at'),
            'topics': topics, 'prior_days': prior,
            'catalog_complete': catalog_complete,
            'session_kind': 'eod' if eod and eod.get('source_as_of') else None,
            'source_as_of': (eod or {}).get('source_as_of'),
            'collected_at': (eod or {}).get('collected_at'),
            'input_gaps': gaps}


def precompute(payload, plate_type):
    payload = copy.deepcopy(payload)
    if plate_type not in (14, 15, 17, 18) or payload.get('taxonomy_version') != ml_r1_service.taxonomy_for(plate_type):
        raise ValueError('分类版本未验证')
    if payload.get('source') not in ml_r1.SOURCE_ALLOWLIST or not payload.get('source_evidence'):
        raise ValueError('缺少独立来源证据')
    date = payload['date']
    if plate_type in (17, 18):
        from app.services import ml_catalog
        catalog = ml_catalog.get(date, payload['catalog_snapshot_id']) if payload.get('catalog_snapshot_id') else None
        if not catalog:
            payload['catalog_complete'] = False
            payload.setdefault('input_gaps', []).append('missing_target_catalog_snapshot')
            for topic in payload.get('topics', []):
                topic['membership_verified'] = False
        else:
            nodes = {n['code']: n for n in catalog['nodes'] if n['plate_type'] == plate_type}
            topics = payload.get('topics', [])
            if len(topics) != len(nodes) or {t['code'] for t in topics} != set(nodes):
                raise ValueError('计算目录与固定的分类快照不一致')
            for topic in topics:
                node = nodes[topic['code']]
                members = [s['code'] for s in topic.get('members', [])]
                if len(set(members)) != len(members) or set(members) != set(node['members']):
                    raise ValueError('计算成员与固定的分类快照不一致')
                topic.update(name=node['name'], membership_verified=node['membership_status'] == 'verified',
                             membership_known_at=catalog['known_at'], effective_from=date)
            payload['catalog_complete'] = plate_type in catalog['complete_levels']
    calendar = ml_r1_service.calendar_days()
    expected = [d for d in calendar if d < date][-20:]
    if date not in calendar or len(expected) != 20 or payload.get('prior_days') != expected:
        raise ValueError('前置20日必须与发布日历逐日一致')
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    existing = store.ml_r1_snapshot_get(payload['taxonomy_version'], date, plate_type=plate_type)
    if existing and existing.get('snapshot_id') == digest and existing.get('formula_version') == ml_r1.VERSION and existing.get('implementation_version') == '20260915.4':
        return existing
    now = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
    with store._conn() as conn:
        conn.execute('INSERT OR IGNORE INTO ml_r1_inputs VALUES(?,?,?)', (digest, encoded, now))
    keys = ('date', 'topics', 'market', 'actions', 'source', 'source_evidence', 'market_full_universe', 'market_known_at', 'prior_days', 'catalog_complete', 'source_as_of')
    result = ml_r1.compute_day(**{k: payload[k] for k in keys if k in payload})
    # 算法可计算不等于可发布。当前只实现盘后完整日级输入；盘中不得套全天量能。
    try:
        event = datetime.fromisoformat(str(payload.get('source_as_of')).replace('Z', '+00:00'))
        event_ok = event.tzinfo is not None and event <= datetime.now(ZoneInfo('Asia/Shanghai')) and event.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat() == date and event.astimezone(ZoneInfo('Asia/Shanghai')).hour >= 15
    except (ValueError, TypeError):
        event_ok = False
    if payload.get('session_kind') != 'eod' or not event_ok:
        result['status'] = 'missing_input'
        result['reasons'].append('missing_verified_eod_time')
        for row in result['rows']:
            row['status'] = 'missing_input'
            row['strength'] = None
            row['contributions'] = {}
            row['metric_status'] = {k: 'missing_input' for k in ('strength', 'return', 'money')}
            row['reasons'].append('missing_verified_eod_time')
    result.update(implementation_version='20260915.4', snapshot_id=digest, input_snapshot_id=digest, collected_at=payload.get('collected_at'), computed_at=now, available_at=now,
                  taxonomy_version=payload['taxonomy_version'], plate_type=plate_type, catalog_snapshot_id=payload.get('catalog_snapshot_id'))
    result['reasons'] = list(dict.fromkeys(result['reasons'] + payload.get('input_gaps', [])))
    store.ml_r1_snapshot_save(payload['taxonomy_version'], date, result, plate_type=plate_type)
    return result
