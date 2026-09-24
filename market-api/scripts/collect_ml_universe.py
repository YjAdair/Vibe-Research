#!/usr/bin/env python3
"""采集全市场清单点时证据（含f26上市日）→ kv ml_r1:universe:*。"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services import ml_r1_kline

if __name__ == '__main__':
    print(asyncio.run(ml_r1_kline.collect_universe()))
