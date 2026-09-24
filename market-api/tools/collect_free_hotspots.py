"""有界初始化免费热点成员与单板块历史；不读取原站数据。"""
import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.store import store
from app.datasources.http import get_client
from app.services import free_hotspots, hotspot_history


async def run(limit: int, history: bool):
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    # 15 is the independently verified Eastmoney concept taxonomy.  Target
    # 17 is deliberately isolated and returns a status object, not rows.
    rows = await free_hotspots.rank_days(today, 1, 1, limit, 15)
    if isinstance(rows, dict):
        report = {'date': today, 'ranked': 0, 'members': [], 'history': [],
                  'origin_used': False, 'status': rows.get('status', 'missing'),
                  'reason': rows.get('reason') or rows.get('reasons')}
        store.kv_set('free_hotspots_initialization', report)
        await get_client().aclose()
        return report
    days = (store.kv_get('collector_calendar_v1', {}) or {}).get('days', [])
    prior = sorted(d for d in days if d < today)
    report = {'date': today, 'ranked': len(rows), 'members': [], 'history': [], 'origin_used': False}
    failures = 0
    history_stopped = False
    for row in rows:
        code = row['plate_code']
        try:
            member = free_hotspots.membership(code, today)
            if member.get('as_of') != today:
                member = await free_hotspots.collect_members(code)
            report['members'].append({'code': code, 'count': len(member['stocks']), 'effective_date': member.get('effective_date')})
        except Exception as exc:
            failures += 1
            report['members'].append({'code': code, 'error_type': type(exc).__name__})
        if history and prior and not history_stopped:
            h = await hotspot_history.collect(code, 3, row['plate_name'], prior[-1])
            report['history'].append({'code': code, **h})
            if h['status'] != 'available':
                # 首个历史端点组已完成有界探测，停止扩大到其它板块。
                history_stopped = True
        if failures >= 3:
            report['stop_reason'] = 'member_failure_limit_3'
            break
    store.kv_set('free_hotspots_initialization', report)
    await get_client().aclose()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, choices=range(1, 13), default=12)
    parser.add_argument('--history', action='store_true', help='首个历史源失败即停止历史扩散，成员仍继续')
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.limit, args.history)), ensure_ascii=False, indent=2))
