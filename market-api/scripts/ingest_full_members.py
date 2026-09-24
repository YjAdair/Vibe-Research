"""一次性入库：/tmp/capture_full 全量成分快照 -> data/plate17_members_full.json。

来源：原站 /v3/market/plates/17/{code}/stocks/rank?is_real=1&limit=2000&date1=2026-09-11
（含不挂任何子板块的直属成分；子板块并集覆盖不全，801085 为 831 vs 1527）。
商业化前需替换为自有 THS 成分源（见 docs/待解决问题清单.md）。

用法：cd backend && .venv/bin/python scripts/ingest_full_members.py [capture_dir]
"""
import json
import os
import sys
from pathlib import Path

CAPTURE_DIR = Path(sys.argv[1] if len(sys.argv) > 1 else '/tmp/capture_full')
OUT = Path(__file__).resolve().parents[1] / 'data' / 'plate17_members_full.json'
DAY = '2026-09-11'


def main() -> None:
    plates = {}
    errors = []
    for fp in sorted(CAPTURE_DIR.glob('members_full_*.json')):
        code = fp.stem[len('members_full_'):]
        try:
            d = json.loads(fp.read_text(encoding='utf-8'))
        except Exception as exc:  # noqa: BLE001
            errors.append(f'{code}: {exc}')
            continue
        if '_err' in d:
            errors.append(f"{code}: {d['_err']}")
            continue
        data = d.get('data')
        lst = data.get('list') if isinstance(data, dict) else data
        codes = [str(s.get('stock_code')) for s in (lst or []) if s.get('stock_code')]
        if codes:
            plates[code] = {'captured_at': DAY, 'stocks': codes}
    payload = {
        'meta': {
            'source': 'api.zizizaizai.com /v3/market/plates/17/{code}/stocks/rank?is_real=1&limit=2000 (read-only snapshot)',
            'captured_at': DAY,
            'note': 'Full THS concept membership incl. direct members not covered by sub-plate union. Replace with own curation before commercialization.',
        },
        'plates': plates,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, OUT)
    print(f'plates: {len(plates)}, errors: {len(errors)} -> {OUT}')
    for e in errors[:10]:
        print('ERR', e)


if __name__ == '__main__':
    main()
