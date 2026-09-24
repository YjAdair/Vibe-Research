"""只补已存在THS板块的年度缺口；一次/板块，连续三错停止。"""
import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from app.core.store import store
from app.datasources.ths_board import fetch_board_bars
from tools.audit_hotspot_year import audit
from pathlib import Path

async def main():
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    root = Path(__file__).resolve().parents[2]
    year = now.year
    coverage = audit(root/'backend/zzquant.db', year, now)
    expected = set(coverage['ths_independent']['ohlc']['count_by_date'])
    report = {'year': year, 'source': 'ths_independent', 'saved': [], 'failed': [], 'skipped_complete': 0, 'status': 'running'}
    path = root / f'docs/THS{year}年度补数.json'
    failures = 0
    for stat in store.ths_board_daily_stats():
        code = stat['board_code']
        existing = store.ths_board_daily_range(code, f'{year}-01-01', f'{year}-12-31')
        if expected <= {r['date'] for r in existing}:
            report['skipped_complete'] += 1
            continue
        try:
            data = await fetch_board_bars(code, years=[year])
            bars = [r for r in data['bars'] if r['date'] <= now.date().isoformat()]
            store.ths_board_daily_save(code, bars)
            covered = {r['date'] for r in store.ths_board_daily_range(code, f'{year}-01-01', f'{year}-12-31')}
            report['saved'].append({'code': code, 'missing_dates': sorted(expected - covered)})
            failures = 0
        except Exception as exc:
            failures += 1
            report['failed'].append({'code': code, 'error_type': type(exc).__name__, 'attempts': 1})
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        if failures >= 3:
            report['status'] = 'stopped_after_three_errors'
            break
        await asyncio.sleep(.25)
    else:
        report['status'] = 'finished'
    report['finished_at'] = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'status': report['status'], 'saved': len(report['saved']), 'failed': len(report['failed']), 'skipped_complete': report['skipped_complete']}))

if __name__ == '__main__':
    asyncio.run(main())
