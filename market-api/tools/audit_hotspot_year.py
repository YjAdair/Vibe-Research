"""只读统计热点年度覆盖，不将供应商、成员或指标混为一谈。"""
import argparse
import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def valid(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def summarize(rows, days, metrics):
    # rows[(code, day)]，一个日期存在一条不意味着全板块覆盖。
    result = {}
    codes = sorted({code for code, _ in rows})
    for name, fields in metrics.items():
        counts = {day: 0 for day in days}
        by_code = {code: set() for code in codes}
        for (code, day), row in rows.items():
            if day in counts and all(valid(row.get(field)) for field in fields):
                counts[day] += 1
                by_code[code].add(day)
        result[name] = {'days_with_any_data': sum(n > 0 for n in counts.values()),
                        'missing_dates': [d for d, n in counts.items() if n == 0],
                        'count_by_date': counts,
                        'observed_codes': len(codes),
                        'observed_codes_with_every_day': sum(len(v) == len(days) for v in by_code.values()),
                        'note': '仅统计已观察代码；历史应有板块全集未核实，不能据此宣称全市场完整。'}
    return result


def audit(path, year, now=None):
    now = now or datetime.now(ZoneInfo('Asia/Shanghai'))
    today = now.date().isoformat()
    start = f'{year}-01-01'
    conn = sqlite3.connect(f'{Path(path).resolve().as_uri()}?mode=ro', uri=True)
    try:
        cal = conn.execute("SELECT value FROM kv WHERE key='collector_calendar_v1'").fetchone()
        raw_days = json.loads(cal[0]).get('days', []) if cal else []
        days = []
        for raw in raw_days:
            try:
                d = datetime.strptime(str(raw).replace('-', ''), '%Y%m%d').date().isoformat()
            except ValueError:
                continue
            if start <= d <= f'{year}-12-31' and (d < today or (d == today and now.hour >= 16)):
                days.append(d)
        days = sorted(set(days))
        report = {'year': year, 'checked_at': now.isoformat(), 'expected_days': len(days),
                  'start': days[0] if days else None, 'end': days[-1] if days else None,
                  'calendar_basis': 'collector_calendar_v1; 当日16点前不视为完成', 'eastmoney': {}}
        for kind in (2, 3):
            rows = {}
            for table in ('board_daily_history', 'board_snapshots'):
                for code, day, payload in conn.execute(f'SELECT plate_code,trade_date,payload FROM {table} WHERE board_type=? AND trade_date BETWEEN ? AND ?', (kind, start, today)):
                    rows[(code, day)] = {**rows.get((code, day), {}), **json.loads(payload)}
            report['eastmoney'][str(kind)] = summarize(rows, days, {'pct': ['pct'], 'main_net_inflow': ['main_net_inflow'], 'ohlc': ['open', 'price', 'high', 'low']})
        rows = {(c, d): json.loads(p) for c, d, p in conn.execute('SELECT board_code,trade_date,payload FROM ths_board_daily WHERE trade_date BETWEEN ? AND ?', (start, today))}
        report['ths_independent'] = summarize(rows, days, {'ohlc': ['open', 'close', 'high', 'low']})
        stock_counts = dict(conn.execute('SELECT trade_date,count(*) FROM daily_close WHERE close IS NOT NULL AND trade_date BETWEEN ? AND ? GROUP BY trade_date', (start, today)))
        report['stock_close'] = {'count_by_date': {d: stock_counts.get(d, 0) for d in days}, 'missing_dates': [d for d in days if not stock_counts.get(d)], 'note': '仅日收盘覆盖，不证明复权和当时成员完整。'}
        popular = {d[0] for d in conn.execute('SELECT trade_date FROM popular_snapshots')}
        report['popular'] = {'missing_dates': [d for d in days if d not in popular], 'days_with_snapshot': sum(d in popular for d in days), 'note': '有快照不证明榜单完整或可用于所有板块。'}
        member_days = []
        for key, payload in conn.execute("SELECT key,value FROM kv WHERE key LIKE 'free_hotspots:members:%'"):
            obj = json.loads(payload)
            if isinstance(obj, dict):
                day = obj.get('effective_date') or obj.get('as_of')
                if day:
                    member_days.append(str(day)[:10])
        report['free_members'] = {'earliest_observed_date': min(member_days) if member_days else None, 'note': '此日期之前不允许以当前成分补历史。'}
        report['complete'] = False
        report['note'] = '未修改数据库、未联网。各供应商独立，缺失不等于零；此报告不是价格真实性或回测验收。'
        return report
    finally:
        conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=str(Path(__file__).resolve().parents[1] / 'zzquant.db'))
    parser.add_argument('--year', type=int, default=datetime.now().year)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    report = audit(args.db, args.year)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('year', 'expected_days', 'start', 'end', 'free_members')}, ensure_ascii=False))
