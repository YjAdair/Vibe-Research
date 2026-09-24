#!/usr/bin/env python3
"""独立目标题材证据快照导入；默认只校验，--apply才写入不可变KV。"""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.services.ml_catalog import ingest
if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path);parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    result=ingest(json.loads(args.input.read_text()),apply=args.apply)
    print(json.dumps(result,ensure_ascii=False))
