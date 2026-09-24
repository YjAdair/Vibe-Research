#!/usr/bin/env python3
"""全部板块（行业+概念）成员点时采集 → free_hotspots:members:* kv。

用法: .venv/bin/python scripts/collect_all_board_members.py [YYYY-MM-DD]
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services import ml_r1_kline


async def main() -> None:
    day = sys.argv[1] if len(sys.argv) > 1 else datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    cat = ml_r1_kline.board_codes_for(day)
    codes = sorted(set(cat.get('14', []) + cat.get('15', [])))
    print(f'day={day} industry={len(cat.get("14", []))} concept={len(cat.get("15", []))} boards={len(codes)}', flush=True)
    if not codes:
        raise SystemExit('no complete board catalog for day')
    res = await ml_r1_kline.collect_all_members(codes, concurrency=6)
    print({k: v for k, v in res.items() if k != 'errors'}, flush=True)
    if res.get('errors'):
        print('first errors:', res['errors'][:10], flush=True)

if __name__ == '__main__':
    asyncio.run(main())
