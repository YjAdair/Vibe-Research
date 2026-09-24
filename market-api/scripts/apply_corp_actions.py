#!/usr/bin/env python3
"""按经验证证据回填 ML-R1 总收益因子，不覆盖原始 OHLCV。"""
from __future__ import annotations

import argparse
import copy
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.store import store
from app.services.ml_r1_actions import return_evidence


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _eod_reference(row: sqlite3.Row) -> float | None:
    if not str(row['source'] or '').startswith('eastmoney_snapshot_eod'):
        return None
    meta = _json_object(row['input_meta'])
    evidence = meta.get('official_reference')
    if isinstance(evidence, dict):
        verified = evidence.get('verified') is True
        value = evidence.get('value', row['prev_close'])
    else:
        verified = any(meta.get(key) is True for key in (
            'official_reference_verified', 'official_prev_close_verified'))
        nested = meta.get('official_evidence') or meta.get('evidence')
        if isinstance(nested, dict) and nested.get('verified') is True:
            verified = True
        value = row['prev_close']
    if not verified:
        return None
    try:
        value = float(value)
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def _record_for_interval(record, previous_day: str | None, day: str) -> dict | None:
    if not isinstance(record, dict):
        return None
    events = record.get('events')
    if not isinstance(events, list):
        return record
    selected = [e for e in events if isinstance(e, dict) and isinstance(e.get('ex_date'), str)
                and (previous_day is None or previous_day < e['ex_date'] <= day)]
    result = copy.deepcopy(record)
    result['events'] = selected
    return result


def _codes_arg(values: list[str] | None) -> set[str] | None:
    if not values:
        return None
    out = set()
    for value in values:
        out.update(x.strip() for x in value.split(',') if x.strip())
    return out


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument('--code', action='append', help='只处理一个或多个代码，可逗号分隔')
    ap.add_argument('--end', '--until', dest='end', help='只处理不晚于该交易日 YYYY-MM-DD')
    ap.add_argument('--max-minutes', type=float, default=10, help='运行截止，最多120分钟')
    ap.add_argument('--dry-run', action='store_true', help='只计算和输出报告，不写数据库')
    ap.add_argument('--report-out', default='logs/ca_apply_report.json')
    args = ap.parse_args(argv)
    selected = _codes_arg(args.code)
    where, params = [], []
    if selected:
        marks = ','.join('?' for _ in selected)
        where.append(f'stock_code IN ({marks})')
        params.extend(sorted(selected))
    if args.end:
        where.append('trade_date <= ?')
        params.append(args.end)
    clause = (' WHERE ' + ' AND '.join(where)) if where else ''
    report = {'codes': 0, 'rows': 0, 'would_update': 0, 'updated': 0,
              'factors': 0, 'action_days': 0, 'missing': 0, 'reasons': {},
              'dry_run': args.dry_run, 'end': args.end,
              'codes_filter': sorted(selected) if selected else None}
    t0 = time.monotonic()
    deadline = t0 + min(120, max(0, args.max_minutes)) * 60
    from app.services.ml_r1_service import calendar_days
    calendar = calendar_days()
    previous_session = dict(zip(calendar[1:], calendar))
    conn = sqlite3.connect(store.path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            'SELECT trade_date, stock_code, close, prev_close, source, input_meta '
            f'FROM stock_kline_daily{clause} ORDER BY stock_code, trade_date', params)
        # ponytail: 单证券/限时修复使用内存暂存；全库迁移应按证券分批提交以限制内存和写锁。
        updates = []
        current_code = None
        previous_day = None
        previous_close = None
        events_record = None
        seen_codes = set()
        for row in rows:
            if time.monotonic() >= deadline:
                report['stopped_at_deadline'] = True
                break
            code, day = row['stock_code'], row['trade_date']
            if code != current_code:
                current_code = code
                previous_day = previous_close = None
                events_record = store.kv_get('corp_actions:' + code)
                seen_codes.add(code)
            report['rows'] += 1
            record = _record_for_interval(events_record, previous_day, day) or {}
            record['previous_trade_day_verified'] = bool(previous_day and previous_session.get(day) == previous_day)
            evidence = return_evidence(
                previous_close=previous_close, close=row['close'],
                official_reference=_eod_reference(row), previous_day=previous_day,
                day=day, record=record)
            meta = _json_object(row['input_meta'])
            meta.update({'return_basis': evidence['basis'],
                         'raw_prev_close': evidence['raw_prev_close'],
                         'action_reason': evidence['reason']})
            updates.append((evidence['factor'], int(evidence['action_day']),
                            json.dumps(meta, ensure_ascii=False, sort_keys=True), day, code))
            if evidence['factor'] is None:
                report['missing'] += 1
            else:
                report['factors'] += 1
            if evidence['action_day']:
                report['action_days'] += 1
            reason = evidence['reason'] or 'ok'
            report['reasons'][reason] = report['reasons'].get(reason, 0) + 1
            previous_day, previous_close = day, row['close']
        report['codes'] = len(seen_codes)
        report['would_update'] = len(updates)
        if updates and not args.dry_run:
            conn.executemany(
                'UPDATE stock_kline_daily SET tr_factor=?, action_day=?, input_meta=? '
                'WHERE trade_date=? AND stock_code=?', updates)
            conn.commit()
            report['updated'] = len(updates)
    finally:
        conn.close()
    report['elapsed_s'] = round(time.monotonic() - t0, 3)
    if args.report_out:
        path = Path(args.report_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print('DRY-RUN' if args.dry_run else 'UPDATED', report)
    return report


if __name__ == '__main__':
    main()
