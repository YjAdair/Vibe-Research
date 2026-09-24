"""一次性入库：/tmp/capture 的 matrix/members 快照 + 龙头池种子 -> SQLite 三表。

来源（只读快照，商业化前需替换为自有源，见 docs/待解决问题清单.md）：
- matrix_{pt}_{day}.json  <- /market/plates/{pt}/rank/popular?date1={day}&top_n=12
- members_{pt}_{day}_{code}.json <- /v3/market/plates/{pt|15}/{code}/stocks/rank?is_real=1&limit=100&date1={day}
  （pt25 下钻原站 400，经 pt15 抓取，文件名按 25 存）
- app/datasources/king_pool_seed.json <- 原站龙头池三日快照

用法：cd backend && .venv/bin/python scripts/ingest_captured_snapshots.py [capture_dir]
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.store import store  # noqa: E402

CAPTURE_DIR = Path(sys.argv[1] if len(sys.argv) > 1 else '/tmp/capture')
SEED = Path(__file__).resolve().parents[1] / 'app' / 'datasources' / 'king_pool_seed.json'
MATRIX_RE = re.compile(r'^matrix_(\d+)_(\d{4}-\d{2}-\d{2})\.json$')
MEMBERS_RE = re.compile(r'^members_(\d+)_(\d{4}-\d{2}-\d{2})_(\d{6})\.json$')


def _load(fp: Path):
    try:
        d = json.loads(fp.read_text(encoding='utf-8'))
    except Exception as exc:  # noqa: BLE001
        return None, f'{fp.name}: bad json {exc}'
    if not isinstance(d, dict) or '_err' in d:
        return None, f"{fp.name}: {d.get('_err') if isinstance(d, dict) else 'not dict'}"
    code = d.get('code')
    if code not in (200, 20000):
        return None, f'{fp.name}: bad code {code}'
    data = d.get('data')
    if not isinstance(data, list):
        return None, f'{fp.name}: empty/invalid data'
    return data, None


def main() -> None:
    errors = []
    n_pop = n_mem = 0
    for fp in sorted(CAPTURE_DIR.iterdir()):
        m = MATRIX_RE.match(fp.name)
        if m:
            rows, err = _load(fp)
            if err:
                errors.append(err)
                continue
            store.plate_popular_save(int(m.group(1)), m.group(2), rows)
            n_pop += 1
            continue
        m = MEMBERS_RE.match(fp.name)
        if m:
            rows, err = _load(fp)
            if err:
                errors.append(err)
                continue
            store.plate_members_save(int(m.group(1)), m.group(2), m.group(3), rows)
            n_mem += 1
    n_king = 0
    if SEED.exists():
        seed = json.loads(SEED.read_text(encoding='utf-8'))
        for day, rows in seed.items():
            if isinstance(rows, list) and rows:
                store.king_pool_save(day, rows)
                n_king += 1
            else:
                errors.append(f'king seed {day}: empty rows')
    print(f'plate_popular: {n_pop}, plate_members: {n_mem}, king_pool: {n_king}, errors: {len(errors)}')
    for e in errors[:20]:
        print(' ERR', e)


if __name__ == '__main__':
    main()
