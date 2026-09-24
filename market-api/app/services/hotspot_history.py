"""按需补齐单个免费板块的真实日线；付费仅接受已批准的同源同代码契约。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.store import store
from app.core.cache import cache
from app.datasources import board_history
from app.datasources.qveris_gateway import QVerisGateway, QVerisUnavailable, QVerisGatewayError
from app.services.data_fallback import resolve

TZ = ZoneInfo('Asia/Shanghai')
_locks: dict[str, asyncio.Lock] = {}
CAPABILITY = 'eastmoney_board_daily'


async def collect(code: str, board_type: int, name: str, end: str, *, days: int = 180) -> dict:
    """每次至多一个板块/180自然日，不自动全库付费回填。

    QVeris绑定尚需真实响应验收：允许在已确认的default_parameters中使用
    {code}/{start}/{end}，不猜测工具的参数名。返回值必须通过东财原生
    code和OHLC解析器校验，跨供应商或申万同号序列不会静默替代。
    """
    if board_type not in (2, 3):
        raise ValueError('Unsupported board type')
    end = datetime.strptime(end, '%Y-%m-%d').date().isoformat()
    start = (datetime.fromisoformat(end) - timedelta(days=min(max(days, 1), 180)-1)).date().isoformat()
    key = f'free_hotspot_history:{code}:{start}:{end}'
    async with _locks.setdefault(code, asyncio.Lock()):
        hit = cache.get(key)
        if hit is not None:
            return hit
        previous = store.kv_get(key)
        if previous and previous.get('manual_probe_stopped'):
            return previous
        if previous and previous.get('checked_at'):
            age = (datetime.now(TZ) - datetime.fromisoformat(previous['checked_at'])).total_seconds()
            if 0 <= age < 300:
                return previous
        free_failure = {}
        async def free():
            try:
                return await board_history.fetch_series(code, start, end)
            except Exception as exc:
                free_failure['free_error_type'] = type(exc).__name__
                raise

        gateway = QVerisGateway()
        paid = None
        try:
            binding = gateway.binding(CAPABILITY)
        except (QVerisUnavailable, QVerisGatewayError):
            binding = None
        if binding is not None:
            async def paid():
                context = {'code': code, 'start': start, 'end': end}
                params = {k: v.format_map(context) if isinstance(v, str) else v
                          for k, v in binding.default_parameters.items()}
                raw = await gateway.execute(CAPABILITY, params, max_response_size=131072)
                return board_history.parse_series(raw, code, start, end)

        def valid(rows):
            if not isinstance(rows, list) or not rows:
                return False
            dates = [r.get('date1') for r in rows]
            return len(set(dates)) == len(dates) and all(start <= d <= end for d in dates)

        outcome = await resolve(CAPABILITY, free, valid, paid_fetch=paid, cache_key=key)
        if outcome['ok']:
            now = datetime.now(TZ).isoformat()
            rows = [{**r, 'plate_code': code, 'plate_name': name, 'plate_type': board_type,
                     'source': 'eastmoney' if outcome['source'] == 'free' else 'qveris_eastmoney_binding',
                     'data_kind': 'historical_daily', 'collected_at': now,
                     'universe_as_of': now[:10], 'name_basis': 'current_catalog',
                     'score': None, 'rate': r.get('pct'), 'trade_money': r.get('amount')}
                    for r in outcome['data']]
            await asyncio.to_thread(store.board_history_save, board_type, code, rows)
            report = {'status': 'available', 'source': outcome['source'], 'rows': len(rows),
                      'as_of_date': max(r['date1'] for r in rows), 'collected_at': now}
            # A paid bridge is short-lived too: the next refresh tries free again.
            cache.set(key, report, 300)
        else:
            report = {'status': 'unavailable', 'reason': outcome['reason'], 'paid_attempted': outcome['paid_attempted'], **free_failure}
            cache.set(key, report, 300)
        report['checked_at'] = datetime.now(TZ).isoformat()
        await asyncio.to_thread(store.kv_set, key, report)
        return report
