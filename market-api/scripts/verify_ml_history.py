#!/usr/bin/env python3
"""交叉核验已取得的新浪历史响应，默认只读；--apply追加证据，不改价。"""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.services.ml_history import verify_record

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path);parser.add_argument('--apply',action='store_true')
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    result=verify_record(json.loads(args.input.read_text()),apply=args.apply)
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False))
