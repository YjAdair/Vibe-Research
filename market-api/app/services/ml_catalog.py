"""目标题材的独立证据快照。只读/显式导入，不从目标站旧数据推断成员。"""
from __future__ import annotations
import copy
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from app.core.store import store
from app.services import ml_r1

VERSION = 'target_ml_v1'
PREFIX = 'ml_r1:target_catalog:'
TZ = ZoneInfo('Asia/Shanghai')


def _time(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if stamp.tzinfo is not None:
            return stamp
    except (TypeError, ValueError):
        pass
    raise ValueError('时点必须包含日期和时区')


def _independent(source, reference):
    if source not in ml_r1.SOURCE_ALLOWLIST or not isinstance(reference, str) or not reference.strip():
        raise ValueError('缺少独立来源或证据引用')
    if any(x in reference.lower() for x in ('zizizaizai', 'plate17_members_full', 'capture_full')):
        raise ValueError('目标站行情/成员不能作为独立产品数据证据')


def validate(payload):
    """核验结构与时序；来源内容的真实性仍须人工复核，不能凭字段认证供应商。"""
    if not isinstance(payload, dict):
        raise ValueError("目录快照必须为对象")
    record = copy.deepcopy(payload)
    if record.get('taxonomy_version') != VERSION:
        raise ValueError('未知目标题材分类版本')
    date = record.get('trade_date')
    if not isinstance(date, str) or datetime.strptime(date, '%Y-%m-%d').date().isoformat() != date:
        raise ValueError('trade_date必须为YYYY-MM-DD')
    known = _time(record.get('known_at'))
    collected = _time(record.get('collected_at'))
    sample = record.get('knowledge_basis') == 'retrospective_disclosure_sample'
    if known > collected or collected > datetime.now(TZ) or (not sample and not ml_r1.known_before(record['known_at'], date + 'T09:30:00')):
        raise ValueError('目录/成员必须在该日开盘前可知，采集时点不能在未来')
    _independent(record.get('source'), record.get('source_evidence'))
    if record.get('definition_status') != ('sample' if sample else 'verified') or not record.get('definition_ref') or not record.get('reviewed_by'):
        raise ValueError('分类定义和供应商范围尚未经过证据复核')
    complete = record.get('complete_levels', [])
    if not isinstance(complete, list) or any(t not in (17, 18) for t in complete):
        raise ValueError('complete_levels仅允许17/18，不能由目录条数推断完整')
    record['complete_levels'] = complete
    if sample and complete:
        raise ValueError('事后整理的业务关联样本不能声明完整分类')
    nodes = record.get('nodes')
    if not isinstance(nodes, list) or not nodes:
        raise ValueError('目录不能为空')
    keys = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError('目录节点必须为对象')
        typ, code = node.get('plate_type'), node.get('code')
        if typ not in (17, 18) or not isinstance(code, str) or not re.fullmatch(r'80\d{4,6}', code) or not node.get('name') or (typ, code) in keys:
            raise ValueError('分类/代码/名称无效或重复；禁止隐式BK映射')
        keys.add((typ, code))
        if node.get('membership_status') not in ('verified', 'partial', 'missing'):
            raise ValueError('成员状态必须明确为verified、partial或missing')
        if sample and node['membership_status']=='verified':
            raise ValueError('事后关联样本不得声明正式历史成员')
        members = node.get('members')
        if not isinstance(members, list) or any(not isinstance(c, str) or not re.fullmatch(r'\d{6}', c) for c in members) or len(set(members)) != len(members):
            raise ValueError('成员必须为不重复的六位证券代码列表')
        if node['membership_status'] in ('verified','partial'):
            _independent(node.get('source'), node.get('membership_evidence_ref'))
            if node['membership_status']=='verified' and node.get('expected_members') != len(members):
                raise ValueError('完整成员声明与实际条数不一致')
            if node['membership_status']=='partial' and (node.get('expected_members') is not None or not members):
                raise ValueError('部分独立证据须有成员且完整数量未知，不能以样本数冒充总数')
            if sample:
                evidence = node.get('member_evidence') or {}
                if set(evidence) != set(members):
                    raise ValueError('关联样本必须逐证券提供证据')
                for item in evidence.values():
                    _independent(node.get('source'),item.get('reference'))
                    if not ml_r1.known_before(item.get('published_at'),date+'T09:30:00'):
                        raise ValueError('样本不能包含所选日期之后才公开的公告')
        elif members:
            raise ValueError('未验证成员不得随缺失目录导入')
    for node in nodes:
        parent = node.get('parent_code')
        if (node['plate_type'] == 18 and (17, parent) not in keys) or (node['plate_type'] == 17 and parent is not None):
            raise ValueError('一级/二级父子关系无效')
    return record


def ingest(payload, *, apply=False):
    record = validate(payload)
    key = store.kv_append_snapshot(PREFIX + VERSION + ':' + record['trade_date'], record) if apply else None
    return {'status': 'stored' if apply else 'validated_not_stored', 'catalog_snapshot_id': key,
            'date': record['trade_date'], 'nodes': len(record['nodes']),
            'complete_levels': record['complete_levels']}


def get(date, snapshot_id=None):
    prefix = PREFIX + VERSION + ':' + date + ':'
    keys = [snapshot_id] if snapshot_id else store.kv_get(prefix + 'index', []) or []
    records = []
    for key in keys:
        if not isinstance(key, str) or not key.startswith(prefix) or key.endswith(':index'):
            continue
        record = store.kv_get(key)
        try:
            record = validate(record or {})
        except (TypeError, ValueError, KeyError):
            continue
        if record['trade_date'] == date:
            records.append({**record, 'catalog_snapshot_id': key})
    return max(records, key=lambda r: (r['definition_status']=='verified',_time(r['known_at']), _time(r['collected_at']))) if records else None


def nodes(date, plate_type, snapshot_id=None):
    record = get(date, snapshot_id)
    return [n for n in (record or {}).get('nodes', []) if n['plate_type'] == plate_type], record


def membership(date, plate_type, code, snapshot_id=None):
    values, record = nodes(date, plate_type, snapshot_id)
    node = next((n for n in values if n['code'] == code), None)
    ready = node is not None and node['membership_status'] == 'verified'
    partial = node is not None and node['membership_status']=='partial'
    return {'stocks': list(node['members']) if ready or partial else [], 'status': 'ok' if ready else 'partial_membership' if partial else 'missing_point_in_time_membership',
            'quotes': {c:{'name':e['security_name']} for c,e in (node or {}).get('member_evidence',{}).items() if isinstance(e.get('security_name'),str)},
            'membership_complete':ready,'expected_members':node.get('expected_members') if node else None,
            'knowledge_basis':(record or {}).get('knowledge_basis','point_in_time'),
            'as_of': date if ready else None, 'effective_date': date if ready else None,
            'collected_at': (record or {}).get('collected_at'), 'known_at': (record or {}).get('known_at'),
            'source': node.get('source') if node else None, 'evidence_ref': node.get('membership_evidence_ref') if node else None,
            'catalog_snapshot_id': (record or {}).get('catalog_snapshot_id'), 'taxonomy_version': VERSION,
            'plate_type': plate_type, 'plate_code': code, 'parent_code': node.get('parent_code') if node else None}


def prepared_membership(date, plate_type, code):
    """查询绑定已发布输入引用的目录；未准备输入时允许独立成员支持行情浏览。"""
    snap = store.ml_r1_snapshot_get(VERSION, date, plate_type=plate_type)
    parent_snap = store.ml_r1_snapshot_get(VERSION, date, plate_type=17) if plate_type == 18 else None
    pinned = (snap or parent_snap or {}).get('catalog_snapshot_id')
    if snap and parent_snap and snap.get('catalog_snapshot_id') != parent_snap.get('catalog_snapshot_id'):
        return {'stocks': [], 'status': 'target_catalog_snapshot_mismatch', 'catalog_snapshot_id': None, 'taxonomy_version': VERSION, 'date': date}
    if (snap or parent_snap) and not pinned:
        record = {'stocks': [], 'status': 'missing_point_in_time_membership', 'catalog_snapshot_id': None}
    else:
        record = membership(date, plate_type, code, pinned)
    record.update(formula_id='ml_r1', formula_version=ml_r1.VERSION, taxonomy_version=VERSION,
                  input_snapshot_id=(snap or {}).get('snapshot_id'), date=date)
    return record


def subplates(plate_type, code, dates):
    found, stocks, stats, states = {}, {}, {}, {}
    for date in dates:
        parent = prepared_membership(date, plate_type, code)
        catalog = get(date, parent['catalog_snapshot_id']) if parent.get('catalog_snapshot_id') else None
        stocks[date], stats[date] = {}, {}
        states[date] = {'status': 'available' if catalog and all(t in catalog['complete_levels'] for t in (17,18)) else 'partial' if catalog else 'missing_input',
                        'catalog_snapshot_id': parent.get('catalog_snapshot_id'),
                        'input_snapshot_id': parent.get('input_snapshot_id'),
                        'reasons': [] if catalog else ['missing_target_catalog_snapshot']}
        from app.services import ml_r1_service
        child_snap = ml_r1_service.get_day(18, date) if catalog else {}
        child_rows = {r['plate_code']: r for r in child_snap.get('rows', [])} if child_snap.get('catalog_snapshot_id') == parent.get('catalog_snapshot_id') and ml_r1.verified_close_time(child_snap.get('source_as_of'), date) else {}
        for node in (catalog or {}).get('nodes', []):
            if plate_type != 17 or node['plate_type'] != 18 or node.get('parent_code') != code:
                continue
            found[node['code']] = {'code': node['code'], 'name': node['name'], 'parent_code': code,
                                   'membership_status':node['membership_status'],'expected_members':node.get('expected_members')}
            if node['membership_status'] in ('verified','partial'):
                stocks[date][node['code']] = node['members']
            row = child_rows.get(node['code'], {})
            values = {k: row.get(k) for k in ('quote_rate', 'limit_up_count', 'limit_down_count')}
            stats[date][node['code']] = {**values, 'coverage': row.get('quote_coverage', {}),
                'input_snapshot_id': child_snap.get('snapshot_id'), 'source_as_of': child_snap.get('source_as_of'),
                'status': 'available' if all(v is not None for v in values.values()) else 'partial' if any(v is not None for v in values.values()) else 'missing_input',
                'reasons': [] if all(v is not None for v in values.values()) else ['missing_verified_subplate_quote_statistics']}
    return {'sub_plates': list(found.values()), 'stocks': stocks, 'stats': stats, 'dates': states,
            'status': 'available' if all(v['status'] == 'available' for v in states.values()) else 'partial' if found else 'missing_input',
            'taxonomy_version': VERSION, 'formula_id': 'ml_r1', 'formula_version': ml_r1.VERSION}
