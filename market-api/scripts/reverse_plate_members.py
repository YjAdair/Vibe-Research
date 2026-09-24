"""逆向 801660 类板块的原站全量成分（stocks/rates 分页拉全 752 只）。

与本地子板块并集 diff，输出 21 只跨板块扩展成分的归属信息。
开发期校准用；商业化后由东财/同花顺官方板块映射替代。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ORIG = 'https://api.zizizaizai.com'


async def fetch_all(client, plate_code: str, date1: str) -> list[dict]:
    rows = []
    page = 1
    while True:
        resp = await client.get(
            ORIG + f'/v3/market/plates/17/{plate_code}/stocks/rates',
            params={'date1': date1, 'page': page, 'limit': 100},
            timeout=30,
        )
        body = resp.json()
        if body.get('code') != 200:
            raise RuntimeError('API error %s' % body.get('code'))
        data = body.get('data') or {}
        rows.extend(data.get('list') or [])
        total = data.get('total') or 0
        if len(rows) >= total or not data.get('list'):
            break
        page += 1
        await asyncio.sleep(6.6)
    return rows


async def main():
    import httpx
    from app.services.plate_flow import _plate_members_union
    plate_code = sys.argv[1] if len(sys.argv) > 1 else '801660'
    date1 = sys.argv[2] if len(sys.argv) > 2 else '2026-09-11'
    async with httpx.AsyncClient() as client:
        orig_rows = await fetch_all(client, plate_code, date1)
    orig_codes = {r['stock_code'] for r in orig_rows}
    local_union = _plate_members_union(plate_code, date1)
    extra = sorted(orig_codes - local_union)
    missing = sorted(local_union - orig_codes)
    print('orig total: %d, local union: %d' % (len(orig_codes), len(local_union)))
    print('extra (orig has, local missing): %d' % len(extra))
    for c in extra:
        name = next((r['stock_name'] for r in orig_rows if r['stock_code'] == c), '')
        print('  %s %s' % (c, name))
    print('local-only: %d' % len(missing))
    for c in missing:
        print('  %s' % c)
    Path('/tmp/plate_%s_extra.json' % plate_code).write_text(
        json.dumps({'plate': plate_code, 'date': date1, 'extra': extra, 'missing': missing}, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    asyncio.run(main())
