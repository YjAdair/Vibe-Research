"""回填历史龙虎榜席位明细（东财 datacenter 免费接口）。

用法: python scripts/backfill_lhb_seats.py [days_back]
缺省回填 lhb_snapshots 已有日期中 lhb_seats 缺失的部分，每天约 284 请求。
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.store import store  # noqa: E402
from app.services import lhb  # noqa: E402

THROTTLE = 0.4


def missing_dates() -> list[str]:
    with store._conn() as conn:
        snap_days = [r[0] for r in conn.execute(
            'SELECT trade_date FROM lhb_snapshots ORDER BY trade_date').fetchall()]
        seat_days = {r[0] for r in conn.execute(
            'SELECT DISTINCT trade_date FROM lhb_seats').fetchall()}
    return [d for d in snap_days if d not in seat_days]


async def main():
    dates = missing_dates()
    print('missing dates: %d (%s ~ %s)' % (len(dates), dates[0] if dates else '-', dates[-1] if dates else '-'), flush=True)
    if not dates:
        return
    # 最新的先回填
    for i, d in enumerate(reversed(dates)):
        try:
            rows = await lhb.collect_seats(d)
            lhb.publish_seats(d, rows)
            print('[%d/%d] %s seats=%d' % (i + 1, len(dates), d, len(rows)), flush=True)
        except Exception as exc:
            print('[%d/%d] %s FAIL %s' % (i + 1, len(dates), d, exc), flush=True)
        await asyncio.sleep(THROTTLE)


if __name__ == '__main__':
    asyncio.run(main())
