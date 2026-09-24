# -*- coding: utf-8 -*-
"""从原站导出的题材表 JSON 导入本地 topics 表（id 对齐原站）。

用法: python scripts/import_topic_tables.py /tmp/topic_scores/all_tables.json
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.core.store import store  # noqa: E402


def main(path: str) -> None:
    items = json.load(open(path))
    created = updated = 0
    existing = {t.get('unique_key'): t for t in store.list_topics(include_deleted=True)}
    for it in items:
        key = it.get('unique_key')
        if not key:
            continue
        rec = {
            'id': it.get('id'),
            'unique_key': key,
            'name': it.get('name') or '',
            'content': it.get('content') or '',
            'rows': it.get('rows') or [],
            'today_pct': it.get('today_pct') or 0,
            'up_count': it.get('up_count') or 0,
            'down_count': it.get('down_count'),
            'stock_count': (it.get('up_count') or 0) + (it.get('down_count') or 0),
            'leader_count': it.get('leader_count') or 0,
            'limit_up_count': it.get('limit_up_count') or 0,
            'limit_down_count': it.get('limit_down_count') or 0,
            'selection_scope': 'user',
            'is_top': 1 if it.get('is_top') else 0,
            'is_deleted': 1 if it.get('is_deleted') else 0,
            'created_time': it.get('created_time') or '',
            'updated_time': it.get('updated_time') or '',
        }
        old = existing.get(key)
        if old:
            updated += 1
        else:
            created += 1
        store.upsert_topic(rec)
    print(f'imported: created={created} updated={updated} total_in_file={len(items)}')


if __name__ == '__main__':
    main(sys.argv[1])
