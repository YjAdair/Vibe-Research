"""回填 2026-05-11 至 07-02 缺失的全市场 daily_close（腾讯日K）。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import daily_close


async def main():
    # 腾讯日K 返回最近 n_days+8 根，需要覆盖到 05-11（今天 09-12，约 85 个交易日）
    result = await daily_close.backfill_history(n_days=90, concurrency=15)
    print('days:', result['days'])
    print('errors:', result['errors'], 'stocks:', result['stocks'])
    saved = result['saved']
    for d in sorted(saved):
        print(d, saved[d])

asyncio.run(main())

