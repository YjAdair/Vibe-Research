"""一次性入库：复盘涨停梯队矩阵快照（/review/uplimit/hot 不带 board）。

来源（只读快照，商业化前需替换为自有源，见 docs/待解决问题清单.md）：
- app/datasources/review_uplimit_seed.json  <- 原站多日矩阵快照 {date: data对象}
- [snapshot_dir]/hot_{YYYY-MM-DD}.json      <- 抓取的单日 data 对象（可选，缺省
  /tmp/hot_snapshots；不存在则跳过，种子文件已含全部日期）

用法：cd backend && .venv/bin/python scripts/ingest_review_uplimit.py [snapshot_dir]
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.store import store  # noqa: E402

SEED = Path(__file__).resolve().parents[1] / 'app' / 'datasources' / 'review_uplimit_seed.json'
SNAP_RE = re.compile(r'^hot_(\d{4}-\d{2}-\d{2})\.json$')


def _valid(data) -> bool:
    return (isinstance(data, dict) and isinstance(data.get('plate'), list)
            and isinstance(data.get('plate_stocks'), dict))


def main() -> None:
    snap_dir = Path(sys.argv[1] if len(sys.argv) > 1 else '/tmp/hot_snapshots')
    days: dict[str, dict] = {}
    if SEED.exists():
        seed = json.loads(SEED.read_text(encoding='utf-8'))
        for day, data in seed.items():
            if _valid(data):
                days[day] = data
    if snap_dir.is_dir():
        for fp in sorted(snap_dir.iterdir()):
            m = SNAP_RE.match(fp.name)
            if not m:
                continue
            try:
                data = json.loads(fp.read_text(encoding='utf-8'))
            except Exception as exc:  # noqa: BLE001
                print(f' SKIP {fp.name}: bad json {exc}')
                continue
            if _valid(data):
                days[m.group(1)] = data
            else:
                print(f' SKIP {fp.name}: invalid shape')
    for day in sorted(days):
        store.review_uplimit_save(day, days[day])
        print(f' {day}: plates={len(days[day]["plate"])} '
              f'stocks={len((days[day].get("stocks") or "").split(",")) if days[day].get("stocks") else 0}')
    print(f'review_uplimit_snapshots saved: {len(days)}')


if __name__ == '__main__':
    main()
