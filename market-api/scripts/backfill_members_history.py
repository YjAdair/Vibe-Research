"""回填子板块成分历史快照（原站 sub-plates-stocks 多日期批量回放）。

原站接口 dates 参数可带多日，每板块一次请求拉全部缺失日期。
用法: python scripts/backfill_members_history.py [max_plates]
缺省回填 snapshot 中全部 curated 板块（69 个），每板块 1 请求 + 6.6s 限流。
日期集 = popular_snapshots 全部日期 - 已有日期。
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.store import store  # noqa: E402
from app.services.plate_flow import (  # noqa: E402
    MEMBERS_PATH, load_members, save_members_snapshot,
)
from app.datasources import zizizaizai  # noqa: E402

THROTTLE = 6.6
CHUNK_DAYS = 9  # 每请求日期数上限（87 天 -> 10 请求/板块）


def missing_dates(plate_code: str | None = None) -> list[str]:
    import json
    snapshot = json.loads(MEMBERS_PATH.read_text(encoding='utf-8'))
    if plate_code:
        v = snapshot.get('plates', {}).get(plate_code) or {}
        have = set((v.get('stocks') or {}).keys())
    else:
        have: set[str] = set()
        for v in snapshot.get('plates', {}).values():
            have.update((v.get('stocks') or {}).keys())
    with store._conn() as conn:
        days = [r[0] for r in conn.execute(
            'SELECT trade_date FROM popular_snapshots ORDER BY trade_date').fetchall()]
    return [d for d in days if d not in have]


async def main(max_plates: int | None = None):
    plates = load_members()
    codes = [c for c, v in plates.items() if v.get('sub_plates')]
    if max_plates:
        codes = codes[:max_plates]
    ok = fail = 0
    for pi, code in enumerate(codes):
        dates = missing_dates(code)
        if not dates:
            print('[%s] %d/%d up to date' % (code, pi + 1, len(codes)), flush=True)
            continue
        chunks = [dates[i:i + CHUNK_DAYS] for i in range(0, len(dates), CHUNK_DAYS)]
        saved_days = 0
        for chunk in chunks:
            try:
                data = await zizizaizai.sub_plates_stocks(code, chunk)
            except Exception as exc:
                print('[%s] FAIL %s' % (code, exc), flush=True)
                fail += 1
                if fail >= 10:
                    print('too many failures, aborting', flush=True)
                    return
                time.sleep(THROTTLE)
                continue
            stocks = data.get('stocks') or {}
            for d in chunk:
                if stocks.get(d):
                    n = save_members_snapshot(code, d, {**data, 'stocks': {d: stocks[d]}})
                    saved_days += 1 if n else 0
            time.sleep(THROTTLE)
        ok += 1
        print('[%s] %d/%d plates saved_days=%d' % (code, pi + 1, len(codes), saved_days), flush=True)
    print('DONE plates_ok=%d requests_fail=%d' % (ok, fail), flush=True)


if __name__ == '__main__':
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else None))
