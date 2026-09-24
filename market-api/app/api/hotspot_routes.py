"""热点免费数据读接口；不读取目标站快照，不推断供应商分类等价。"""
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Depends, Request

from app.services import free_hotspots as service
from app.services import pools, ml_catalog, ml_r1_service

def formula_contract(request: Request, formula_version: str | None = None, formula_id: str | None = None, taxonomy_version: str | None = None):
    from app.services.ml_r1 import VERSION
    if formula_version is not None and formula_version != VERSION:
        raise HTTPException(409, '计算口径已更新，请刷新页面后重试')
    validate_formula(formula_id)
    if formula_id == 'ml_r1' and taxonomy_version is not None:
        typ = request.path_params.get('plate_type', request.query_params.get('plate_type'))
        if typ is None and any(part in request.url.path for part in ('/kline/', '/steps', '/plate/popular/reason')):
            typ = 17
        if typ is not None and str(typ).isdigit() and int(typ) in (14, 15, 17, 18) and taxonomy_version != ml_r1_service.taxonomy_for(int(typ)):
            raise HTTPException(409, '分类版本与所选板块不匹配')


router = APIRouter(prefix='/v3/market/hotspots', tags=['free-hotspots'], dependencies=[Depends(formula_contract)])


def day(value: str) -> str:
    try:
        return datetime.strptime(value.replace('-', ''), '%Y%m%d').date().isoformat()
    except ValueError as exc:
        raise HTTPException(422, '无效日期') from exc


def code(value: str, plate_type: int | None = None) -> str:
    import re
    if not re.fullmatch(r'(?:BK\d{4,6}|80\d{4,6})', value):
        raise HTTPException(422, '免费板块接口需要东财BK代码或已观察的目标目录代码，不自动映射')
    if plate_type in (14, 15) and value.startswith('80'):
        raise HTTPException(422, '东财14/15接口只接受东财原生BK代码，不接受目标目录代码')
    if plate_type in (17, 18) and value.startswith('BK'):
        raise HTTPException(422, '目标17/18不接受东财BK代码，不自动映射')
    return value


def result(data, **meta):
    return {'code': 200, 'data': data, 'meta': {
        'source': 'eastmoney_public', 'taxonomy': 'eastmoney_boards',
        'original_score_available': False, 'origin_data_used': False, **meta}}


def unsupported(plate_type: int):
    payload = service.unsupported_taxonomy(plate_type)
    return result(payload, status='unsupported_taxonomy', taxonomy=payload['taxonomy'],
                  target_taxonomy_id=plate_type, source=None)


def native_taxonomy(plate_type: int) -> str:
    return ml_catalog.VERSION if plate_type in (17, 18) else 'eastmoney_industry' if plate_type == 14 else 'eastmoney_concept'


def validate_formula(formula_id, n_days=None):
    if formula_id not in (None, 'ml_r1'):
        raise HTTPException(422, '未知公式版本')
    if formula_id == 'ml_r1' and n_days is not None and n_days not in (1, 3, 5):
        raise HTTPException(422, 'ML-R1仅支持1/3/5日')


@router.get('/plates/{plate_type}/rank/days')
async def rank(plate_type: int, date2: str | None = None,
               n_days: int = Query(1, ge=1, le=5), n_type: int = Query(1),
               limit: int = Query(20, ge=1, le=1000), formula_id: str | None = None,
               taxonomy_version: str | None = None):
    validate_formula(formula_id, n_days)
    if plate_type not in (14, 15, 17, 18) or n_type not in (1, 3, 9):
        raise HTTPException(422, '不支持的分类或排序')
    end = day(date2) if date2 else datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    if formula_id == 'ml_r1':
        from app.services import ml_r1_service
        data = ml_r1_service.rank(plate_type, end, n_days, n_type, limit, taxonomy_version)
        return result(data, status=data.get('status') if isinstance(data, dict) else 'available', formula_id='ml_r1', taxonomy=ml_r1_service.taxonomy_for(plate_type), source=None)
    rows = await service.rank_days(end, n_days, n_type, limit, plate_type)
    if isinstance(rows, dict):
        return result(rows, status=rows.get('status'), taxonomy=rows.get('taxonomy'), source=None)
    return result(rows, status='unsupported_metric' if n_type == 9 else ('available' if rows else 'missing'), taxonomy=native_taxonomy(plate_type))


@router.get('/plates/{plate_type}/rank/batch')
async def rank_batch(plate_type: int, dates: str,
                     n_days: int = Query(1, ge=1, le=5), n_type: int = Query(1),
                     limit: int = Query(12, ge=1, le=1000), formula_id: str | None = None,
                     taxonomy_version: str | None = None):
    validate_formula(formula_id, n_days)
    if plate_type not in (14, 15, 17, 18) or n_type not in (1, 3, 9):
        raise HTTPException(422, '不支持的分类或排序')
    selected = [day(d.strip()) for d in dates.split(',') if d.strip()]
    if not selected or len(selected) > 120:
        raise HTTPException(422, '批量日期最多120个且不能为空')
    if formula_id == 'ml_r1':
        from app.services import ml_r1_service
        data = ml_r1_service.rank_batch(plate_type, selected, n_days, n_type, limit, taxonomy_version)
        return result(data, status=data['status'], formula_id='ml_r1', taxonomy=ml_r1_service.taxonomy_for(plate_type), source=None)
    taxonomy = native_taxonomy(plate_type)
    if plate_type in (17, 18):
        data = {'status': 'unsupported_taxonomy', 'columns': [{'date': d, 'status': 'unsupported_taxonomy', 'rows': []} for d in selected]}
        return result(data, status='unsupported_taxonomy', source=None)
    columns = []
    for d in selected:
        try:
            rows = await service.rank_days(d, n_days, n_type, limit, plate_type)
            columns.append({'date': d, 'status': 'unsupported_metric' if n_type == 9 else 'available' if rows else 'missing', 'rows': rows})
        except Exception as exc:
            columns.append({'date': d, 'status': 'error', 'rows': [], 'error': type(exc).__name__})
    return result({'columns': columns, 'meta': {'taxonomy': taxonomy}}, status='available', taxonomy=taxonomy)


@router.get('/capabilities')
async def capabilities(date: str | None = None, plate_type: int = 15):
    from app.core.store import store
    from app.services import ml_r1, ml_r1_service, ml_r1_pipeline
    if plate_type not in (14, 15, 17, 18):
        raise HTTPException(422, '无效板块分类')
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    today = now.date().isoformat()
    dates = [d for d in ml_r1_service.calendar_days() if d < today or (d == today and now.hour >= 15)]
    selected = day(date) if date else dates[-1] if dates else today
    snap = ml_r1_service.get_day(plate_type, selected)
    stored_dates = store.ml_r1_snapshot_dates(ml_r1_service.taxonomy_for(plate_type), plate_type=plate_type)
    history = {d: ml_r1_service.get_day(plate_type, d) for d in stored_dates if d <= selected}
    metrics = {}
    for metric in ('strength', 'return', 'money'):
        states = [r.get('metric_status', {}).get(metric) for r in snap.get('rows', [])]
        final_count = states.count('final')
        preview_count = states.count('partial_preview')
        final_dates = [d for d, item in history.items() if any(r.get('metric_status', {}).get(metric) == 'final' for r in item.get('rows', []))]
        preview_dates = [d for d, item in history.items() if any(r.get('metric_status', {}).get(metric) == 'partial_preview' for r in item.get('rows', []))]
        status = 'final' if states and final_count == len(states) else 'partial_preview' if final_count or preview_count else 'missing_input'
        reasons = list(dict.fromkeys(snap.get('reasons', []) + [reason for r in snap.get('rows', []) for reason in r.get('reasons', [])]))
        common = {'missing_persisted_snapshot','missing_target_catalog_snapshot','formula_version_mismatch','missing_point_in_time_membership',
                  'missing_verified_eod_time','taxonomy_version_unverified','missing_security_master',
                  'source_not_allowlisted_or_unproven','missing_trading_calendar','non_trading_date'}
        relevant = common | ({'price_coverage_below_95_percent','missing_corporate_actions'} if metric == 'return'
                             else {'missing_flow_definition'} if metric == 'money' else set(reasons))
        reasons = [r for r in reasons if r in relevant]
        if metric == 'money' and not final_count:
            reasons = list(dict.fromkeys(reasons + ['missing_flow_definition']))
        metrics[metric] = {'status': status, 'latest_final_date': max(final_dates) if final_dates else None,
                           'preview_dates': sorted(preview_dates), 'final_topics': final_count,
                           'preview_topics': preview_count, 'total_topics': len(states),
                           'input_state': 'not_collected' if 'missing_persisted_snapshot' in snap.get('reasons', []) else 'evidence_missing' if status == 'missing_input' else status,
                           'reasons': reasons or ([] if final_count or preview_count else ['missing_metric_inputs'])}
        metrics[metric]['reason_texts'] = [ml_r1_service.reason_text([r]) for r in metrics[metric]['reasons']]
    annual = store.kv_get('ml_r1_annual_coverage', {}) or {today[:4]: {'status': 'not_audited', 'coverage_verified': False}}
    ready = {k: bool(v['final_topics']) for k, v in metrics.items()}
    catalog = ml_catalog.get(selected, snap.get('catalog_snapshot_id'))
    codes = sorted({c for n in (catalog or {}).get('nodes',[]) if n['plate_type']==plate_type for c in n['members']})
    from app.services.ml_history import complete_daily_bar
    bars = store.kline_by_codes(selected,codes)
    valid_prices = sum(complete_daily_bar(bars.get(c),selected) for c in codes)
    return result({'date': selected, 'plate_type': plate_type, 'metrics': metrics,
                   'taxonomies': {str(t): {'implemented': True, 'taxonomy_version': ml_r1_service.taxonomy_for(t), 'status': 'available' if t in (14, 15) or t in (catalog or {}).get('complete_levels',[]) else 'partial' if catalog else 'missing_input', 'coverage_verified': False} for t in (14, 15, 17, 18)},
                   'historical_prices':{'scope':'selected_target_disclosure_sample','verified_close_count':valid_prices,'disclosed_sample_count':len(codes),
                       'status':'available_for_sample' if valid_prices and valid_prices==len(codes) else 'partial' if valid_prices else 'missing_input',
                       'knowledge_basis':(catalog or {}).get('knowledge_basis'),'backtest_point_in_time_verified':False},
                   'formulas': {'ml_r1': {'implemented': callable(ml_r1.compute_day), 'data_ready': ready['strength'],
                   'metrics': ready, 'as_of': selected, 'formula_version': ml_r1.VERSION, 'default': True}},
                   'native_catalog': ml_r1_pipeline.native_catalog(14) + ml_r1_pipeline.native_catalog(15),
                   'target_catalog': store.ml_target_catalog(), 'vendor_mappings': store.ml_vendor_mappings(),
                   'membership_validity': store.ml_membership_validity(), 'coverage_verified': False,
                   'annual_coverage': annual,
                   'constraints': {'target_data_imported': False, 'paid_source_called': False, 'kline_sync_history_backfill': False}})


@router.get('/native-catalog')
async def native_catalog(plate_type: int = Query(15)):
    if plate_type not in (14, 15):
        raise HTTPException(422, '无效板块分类')
    from app.services.ml_r1_pipeline import native_catalog as catalog
    rows = catalog(plate_type)
    return result({'items': rows, 'count': len(rows), 'business_ranking': False})


@router.get('/plates/{plate_type}/trend')
async def trend(plate_type: int, plate_code: str, day_start: str, day_end: str,
                formula_id: str | None = None, taxonomy_version: str | None = None):
    if plate_type in (17, 18) and formula_id != 'ml_r1':
        return unsupported(plate_type)
    if plate_type not in (14, 15, 17, 18):
        raise HTTPException(422, '无效板块分类')
    start, end = day(day_start), day(day_end)
    if start > end:
        raise HTTPException(422, '开始日期晚于结束日期')
    validate_formula(formula_id)
    if formula_id == 'ml_r1':
        from app.services import ml_r1_service
        payload = ml_r1_service.trend(plate_type, code(plate_code, plate_type), start, end, taxonomy_version)
        return result(payload, status=payload.get('status'), taxonomy=payload.get('taxonomy_version'), formula_id='ml_r1', source=None)
    return result(await service.trend(code(plate_code, plate_type), start, end),
                  taxonomy=native_taxonomy(plate_type))


@router.get('/plates/{plate_type}/{plate_code}/sub-plates-stocks')
async def subplates(plate_type: int, plate_code: str, dates: str, formula_id: str | None = None):
    validate_formula(formula_id)
    code(plate_code, plate_type)
    days = [day(d.strip()) for d in dates.split(',')]
    if len(days) > 60:
        raise HTTPException(422, '单次最多60个日期')
    if plate_type in (17, 18):
        payload = ml_catalog.subplates(plate_type, plate_code, days)
        return result(payload, status=payload['status'], taxonomy=ml_catalog.VERSION, source=None)
    if plate_type not in (14, 15):
        raise HTTPException(422, '无效板块分类')
    # 公共东财目录不提供与原站等价的父子树；不按同名臆造关系。
    return result({'sub_plates': [], 'stocks': {d: {} for d in days}, 'stats': {},
                   'status': 'hierarchy_unavailable',
                   'note': '免费目录暂未核实与目标站等价的二级层级'},
                  taxonomy=native_taxonomy(plate_type))


@router.get('/plates/{plate_type}/{plate_code}/stocks/rates')
async def rates(plate_type: int, plate_code: str, date1: str,
                page: int = Query(1, ge=1), limit: int = Query(30, ge=1, le=300), formula_id: str | None = None):
    validate_formula(formula_id)
    if plate_type in (17, 18) and formula_id == 'ml_r1':
        member = ml_catalog.prepared_membership(day(date1), plate_type, code(plate_code, plate_type))
        payload = await service.stock_rates(plate_code, day(date1), page, limit, at_open=True,
                                               member_record=member if member['status'] in ('ok','partial_membership') else {})
        payload['meta'].update({k: member.get(k) for k in ('formula_id', 'formula_version', 'taxonomy_version', 'catalog_snapshot_id', 'input_snapshot_id')})
        if member['status'] == 'partial_membership':
            payload['meta'].update(status='partial_membership',membership_complete=False,expected_members=None,
                                   reasons=['incomplete_independent_membership'],ranking_scope='verified_disclosure_sample',
                                   knowledge_basis=member.get('knowledge_basis'),backtest_point_in_time_verified=False)
        elif member['status'] != 'ok':
            payload['meta'].update(status='missing_input', reasons=['missing_point_in_time_membership'])
        return result(payload, status=payload['meta']['status'], taxonomy=ml_catalog.VERSION, source=None)
    if plate_type in (17, 18):
        return unsupported(plate_type)
    if plate_type not in (14, 15):
        raise HTTPException(422, '无效板块分类')
    return result(await service.stock_rates(code(plate_code, plate_type), day(date1), page, limit, at_open=formula_id == "ml_r1"),
                  taxonomy=native_taxonomy(plate_type))


@router.get('/plates/{plate_type}/{plate_code}/stocks/rank')
async def popular(plate_type: int, plate_code: str, date1: str,
                  page: int = Query(1, ge=1), limit: int = Query(30, ge=1, le=300), with_pct: int = 1, formula_id: str | None = None):
    validate_formula(formula_id)
    if plate_type in (17, 18) and formula_id == 'ml_r1':
        member = ml_catalog.prepared_membership(day(date1), plate_type, code(plate_code, plate_type))
        payload = await service.popular_rank(plate_code, day(date1), page, limit, with_pct, at_open=True,
                                               member_record=member if member['status'] == 'ok' else {})
        payload['meta'].update({k: member.get(k) for k in ('formula_id', 'formula_version', 'taxonomy_version', 'catalog_snapshot_id', 'input_snapshot_id')})
        if member['status'] != 'ok':
            payload['meta'].update(status='missing_input', reasons=['missing_point_in_time_membership'])
        return result(payload, status=payload['meta']['status'], taxonomy=ml_catalog.VERSION, source=None)
    if plate_type in (17, 18):
        return unsupported(plate_type)
    if plate_type not in (14, 15):
        raise HTTPException(422, '无效板块分类')
    return result(await service.popular_rank(code(plate_code, plate_type), day(date1), page, limit, with_pct, at_open=formula_id == "ml_r1"),
                  taxonomy=native_taxonomy(plate_type),
                  popularity_source='10jqka_public')


@router.get('/plates/{plate_type}/{plate_code}/stocks/pct/batch')
async def pct(plate_type: int, plate_code: str, dates: str, days: int = Query(10, ge=1, le=60),
              formula_id: str | None = None, taxonomy_version: str | None = None, sub_plate_code: str | None = None):
    if plate_type in (17, 18) and formula_id != 'ml_r1':
        return unsupported(plate_type)
    if plate_type not in (14, 15, 17, 18):
        raise HTTPException(422, '无效板块分类')
    selected = [day(d.strip()) for d in dates.split(',')]
    if len(selected) > 15:
        raise HTTPException(422, '单次最多15个日期')
    validate_formula(formula_id)
    if formula_id == 'ml_r1':
        if days not in (5, 10, 20):
            raise HTTPException(422, 'ML-R1涨幅仅支持5/10/20日')
        from app.services import ml_r1_service
        payload = ml_r1_service.pct(plate_type, code(plate_code, plate_type), selected, days, taxonomy_version)
        if sub_plate_code:
            if plate_type != 17:
                raise HTTPException(422, '二级筛选需绑定一级题材')
            child_code = code(sub_plate_code, 18)
            for date, column in payload.items():
                parent = ml_catalog.prepared_membership(date, 17, plate_code)
                child = ml_catalog.membership(date, 18, child_code, parent['catalog_snapshot_id']) if parent.get('catalog_snapshot_id') else {'status': 'missing_input'}
                if child.get('status') == 'ok' and child.get('parent_code') != plate_code:
                    raise HTTPException(422, '二级题材不属于所选一级')
                if child.get('status') != 'ok' or parent.get('status') != 'ok':
                    column.update(stocks={}, items=[], status='missing_input')
                    column['meta'].update(status='missing_input', reasons=['missing_point_in_time_membership'])
                else:
                    allowed = set(parent['stocks']) & set(child['stocks'])
                    column['items'] = [r for r in column['items'] if r['stock_code'] in allowed]
                    column['stocks'] = {bucket: [r for r in rows if r['stock_code'] in allowed] for bucket, rows in column['stocks'].items()}
                    column['meta']['filter_members'] = len(allowed)
                column['meta'].update(sub_plate_code=child_code, catalog_snapshot_id=parent.get('catalog_snapshot_id'))
        return result(payload, taxonomy=taxonomy_version or ml_r1_service.taxonomy_for(plate_type), formula_id='ml_r1')
    return result(await service.pct_batch(code(plate_code, plate_type), selected, days),
                  taxonomy=native_taxonomy(plate_type),
                  return_basis='unadjusted_price_return', backtest_validated=False)


@router.get('/kline/{plate_code}')
async def kline(plate_code: str, n: int = Query(250, ge=1, le=500), plate_type: int = 17, date2: str | None = None):
    board = code(plate_code, plate_type)
    if plate_type not in (14, 15, 17, 18):
        raise HTTPException(422, '无效板块分类')
    if plate_type in (17, 18):
        end = day(date2) if date2 else datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        payload = ml_r1_service.daily_nav(plate_type, board, end, n)
        payload.update(series_kind='daily_nav', cutoff=end, formula_id='ml_r1', taxonomy_version=ml_catalog.VERSION)
        return result(payload, status=payload['status'], taxonomy=ml_catalog.VERSION, source='ml_r1_prepared_daily_returns')
    from app.core.store import store
    data = await service.kline(board, n, end=day(date2) if date2 else None)
    return result(data, taxonomy=native_taxonomy(plate_type))


@router.get('/steps')
async def steps(board: str, date1: str, plate_type: int = 17, formula_id: str | None = None, sub_plate_code: str | None = None):
    validate_formula(formula_id)
    board, date1 = code(board, plate_type), day(date1)
    if plate_type in (17, 18) and formula_id != 'ml_r1':
        return unsupported(plate_type)
    if plate_type not in (14, 15, 17, 18):
        raise HTTPException(422, '无效板块分类')
    if formula_id != 'ml_r1' and date1 == datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat():
        await service.members_snapshot(board)
    membership = ml_catalog.prepared_membership(date1, plate_type, board) if plate_type in (17, 18) else service.membership(board, date1, at_open=formula_id == 'ml_r1')
    # 目标 17：ml_catalog 无时点成员时回退开盘啦成分（与个股/open 梯队同源），避免整列空梯队
    if plate_type in (17, 18) and membership.get('status') != 'ok':
        from app.services import plate_flow
        kpl = plate_flow.plate_members_full(board, date1) if plate_type == 17 else plate_flow.plate_members(board, date1)
        if kpl:
            membership = {
                **membership,
                'status': 'ok',
                'stocks': sorted(kpl),
                'source': 'kaipanla_members',
                'note': (membership.get('note') or '') + ';fallback_kaipanla_members',
            }
    if sub_plate_code:
        if plate_type != 17:
            raise HTTPException(422, '二级筛选需绑定一级题材')
        child = ml_catalog.membership(date1, 18, code(sub_plate_code, 18), membership.get('catalog_snapshot_id')) if membership.get('catalog_snapshot_id') else {'status': 'missing_input'}
        if child.get('status') == 'ok' and child.get('parent_code') != board:
            raise HTTPException(422, '二级题材不属于所选一级')
        membership['stocks'] = sorted(set(membership.get('stocks', [])) & set(child.get('stocks', [])))
        if child.get('status') != 'ok':
            membership['status'] = 'missing_point_in_time_membership'
        membership['sub_plate_code'] = sub_plate_code
    members = set(membership.get('stocks') or [])
    from app.services.ladder_clean import build_ladder_rows
    built = build_ladder_rows(date1, members)
    rows = []
    for r in built['sealed'] + built['broken']:
        rows.append({
            'stock_code': r['stock_code'], 'stock_name': r['stock_name'],
            'up_limit_keep_times': r['up_limit_keep_times'],
            'up_limit_time': None if r.get('up_limit_time') in (None, '--') else r.get('up_limit_time'),
            'up_limit_type': r['up_limit_type'], 'up_limit_desc': r['up_limit_desc'],
            'interval_limit_count': r.get('interval_limit_count'),
            'interval_days': r.get('interval_days'),
            'fd_max': r.get('fd_max'), 'source': f"ladder_clean:{built['pool_source'] or 'close_seal'}",
        })
    pool_source = built['pool_source']
    clean = built['meta']
    status = (
        'available' if members and (pool_source or rows) else
        'partial' if members and rows else
        'missing'
    )
    note = '公开池∩成分，收盘价校验封板；fd_max/精确烂N为免费源上限'
    if clean.get('supplemented_close_seal') or clean.get('reclassified_to_broken'):
        note += f"；补漏{len(clean.get('supplemented_close_seal') or [])}/改炸{len(clean.get('reclassified_to_broken') or [])}"
    if not pool_source and not rows:
        note = '缺少该日涨停/炸板池快照'
    return result({'plate_stocks': {board: rows}, 'plate': [],
                   'max_count': max((r['up_limit_keep_times'] for r in built['sealed']), default=0) if rows else None,
                   'meta': {'status': status, 'membership': membership,
                            'pool_source': pool_source, 'clean': clean, 'note': note}},
                  taxonomy=native_taxonomy(plate_type))


@router.get('/plate/popular/reason')
async def reasons(plate_code: str, plate_type: int = 17, date1: str | None = None):
    if date1:
        day(date1)
    code(plate_code, plate_type)
    if plate_type not in (14, 15, 17, 18):
        raise HTTPException(422, '无效板块分类')
    return result([], status='unavailable', reasons=['missing_independent_reason_source'], source=None, cutoff=day(date1) if date1 else None, taxonomy=native_taxonomy(plate_type),
                  note='尚未建立可验证的公开新闻到板块映射，不读取原站归因消息')
