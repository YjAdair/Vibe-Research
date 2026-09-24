#!/usr/bin/env python3
"""后台计算：JSON独立输入，或--local --date YYYY-MM-DD核验本地输入；无回源。"""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.ml_r1_pipeline import build_local_input, precompute

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('input', nargs='?')
    parser.add_argument('--local', action='store_true')
    parser.add_argument('--date')
    parser.add_argument('--plate-type', type=int, choices=(14, 15, 17, 18), default=15)
    args = parser.parse_args()
    if args.local:
        if not args.date:
            parser.error('--local需要--date')
        payload = build_local_input(args.date, args.plate_type)
    elif args.input:
        payload = json.loads(Path(args.input).read_text())
    else:
        parser.error('提供本地JSON输入或--local')
    result = precompute(payload, args.plate_type)
    print(json.dumps({k: result[k] for k in ('date', 'status', 'coverage', 'reasons', 'snapshot_id')}, ensure_ascii=False))
