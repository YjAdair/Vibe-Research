#!/usr/bin/env python3
"""Backfill historical plate17 member snapshots from the original site.

Dev-period calibration only (rate-limited original-site API).
Usage: python backfill_plate17_members.py 2026-09-10 [more dates...]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

BASE = 'https://api.zizizaizai.com'
HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
MIN_INTERVAL = 6.6
MEMBERS_PATH = Path(__file__).resolve().parents[1] / 'data' / 'plate17_members_snapshot.json'


def main() -> None:
    dates = sys.argv[1:]
    if not dates:
        print('usage: backfill_plate17_members.py DATE [DATE...]')
        raise SystemExit(2)
    data = json.loads(MEMBERS_PATH.read_text(encoding='utf-8'))
    plates = data.setdefault('plates', {})
    codes = sorted(plates)
    print(f'plates={len(codes)} dates={dates}', flush=True)
    last = 0.0
    with httpx.Client(timeout=30, headers=HEADERS) as cli:
        for date in dates:
            ok = fail = 0
            for i, code in enumerate(codes, 1):
                wait = last + MIN_INTERVAL - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                last = time.monotonic()
                try:
                    r = cli.get(f'{BASE}/v3/market/plates/17/{code}/sub-plates-stocks',
                                params={'dates': date})
                    if r.status_code != 200:
                        fail += 1
                        print(f'[{date}] {code} HTTP {r.status_code}', flush=True)
                        continue
                    stocks = ((r.json().get('data') or {}).get('stocks') or {}).get(date)
                    if stocks is None:
                        fail += 1
                        continue
                    plates.setdefault(code, {}).setdefault('stocks', {})[date] = stocks
                    ok += 1
                except Exception as exc:  # noqa: BLE001
                    fail += 1
                    print(f'[{date}] {code} ERR {exc}', flush=True)
                if i % 10 == 0:
                    print(f'[{date}] {i}/{len(codes)} ok={ok} fail={fail}', flush=True)
                    MEMBERS_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            print(f'[{date}] DONE ok={ok} fail={fail}', flush=True)
    MEMBERS_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    print('saved', flush=True)


if __name__ == '__main__':
    main()
