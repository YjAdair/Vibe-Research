"""ML-R1 独立K线与全市场点时证据链路（免费源：东财公开接口）。

职责：
- collect_universe: 全市场上市清单快照 → kv ml_r1:universe:*（点时证据，含 f26 上市日）。
- update_eod: 收盘后逐证券核验报价与总收益证据，保存当日行和采集修订。
- bars_to_rows: 回填脚本与EOD共享，把 fqt=0 bars(+可选hfq映射) 转成日K行。

- 口径（详见 docs/prd/ML题材轮动计算口径PRD.md）：
- prev_close 只保存官方昨收；实际上一交易日收盘单独记入 input_meta。
- tr_factor 来自共享公司行动模块；现金分红总收益不能用 close/除权参考价替代。
- 除权判据：|官方昨收(d) - close(d-1)| > 0.005。
- 缺行不是停牌；无盘后时点的记录不能发布正式结果。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.store import store
from app.datasources import eastmoney
from app.services import ml_r1

TZ = ZoneInfo('Asia/Shanghai')
EX_TOL = 0.005

_num = ml_r1.number


def _save_universe_record(snap: dict) -> dict:
    """全市场清单点时证据。key 含采集时刻，同日多次采集互不覆盖。"""
    now = datetime.now(TZ)
    rec = {
        'collected_at': now.isoformat(),
        'trade_date': snap['trade_date'],
        'source': snap.get('source', 'eastmoney_clist'),
        'source_as_of': snap.get('source_as_of'),
        'source_as_of_max': snap.get('source_as_of_max'),
        'total': snap.get('total'),
        'hs_count': snap.get('hs_count'),
        'bj_count': snap.get('bj_count'),
        'priced_count': snap.get('priced_count'),
        'unpriced_count': snap.get('unpriced_count'),
        'complete': bool(snap.get('complete')),
        'stocks': [{'code': s['code'], 'name': s.get('name', ''),
                    'list_date': s.get('list_date', '')} for s in snap.get('stocks', [])],
    }
    key = store.kv_append_snapshot('ml_r1:universe', rec)
    return {'key': key, 'total': rec['total'], 'collected_at': rec['collected_at'],
            'list_date_missing': sum(1 for s in rec['stocks']
                                     if not (len(s['list_date']) == 8 and s['list_date'].isdigit()))}


async def collect_universe() -> dict:
    snap = await eastmoney.market_snapshot()
    return _save_universe_record(snap)


def bars_to_rows(code: str, bars: list[dict], hfq_map: dict | None = None, *,
                 keep_from: str | None = None, source: str = 'eastmoney_kline_fqt0') -> list[dict]:
    """fqt=0 日K（升序）→ stock_kline_daily 行。

    hfq_map: 仅保存供应商后复权close，不将其当作总收益证明。
    keep_from: 只保留 >= 该日期的行，但更早的 bar 仍参与除权检测的昨收基准。
    """
    rows: list[dict] = []
    from app.services.ml_r1_actions import return_evidence
    prev_date: str | None = None
    prev_close_bar: float | None = None
    for b in bars:
        d = str(b.get('date') or '')
        close = _num(b.get('close'))
        change = _num(b.get('change'))
        if not d or close is None or close <= 0:
            continue
        official_prev = round(close - change, 3) if change is not None else None
        action_day = bool(official_prev is not None and prev_close_bar is not None
                          and abs(official_prev - prev_close_bar) > EX_TOL)
        # 复权价格标签不构成总收益证明；只有与真实前收一致的普通日可直接计算。
        evidence = return_evidence(previous_close=prev_close_bar, close=close,
                                   official_reference=official_prev, previous_day=prev_date, day=d)
        tr = evidence['factor']
        close_hfq = (hfq_map or {}).get(d)
        if keep_from is None or d >= keep_from:
            rows.append({
                'trade_date': d, 'stock_code': code,
                'open': _num(b.get('open')), 'high': _num(b.get('high')),
                'low': _num(b.get('low')), 'close': close,
                'prev_close': official_prev,
                'volume': _num(b.get('volume')), 'amount': _num(b.get('amount')),
                'tr_factor': round(tr, 8) if tr is not None else None,
                'action_day': 1 if action_day else 0,
                'close_hfq': round(close_hfq, 4) if close_hfq is not None else None,
                'source': source,
                'input_meta': {'return_basis': 'total_return_v1_1' if tr is not None else None,
                               'raw_prev_close': prev_close_bar,
                               'action_reason': evidence['reason'],
                               'official_reference_verified': official_prev is not None},
            })
        prev_date, prev_close_bar = d, close
    return rows


async def update_eod(day: str) -> dict:
    """逐证券确认盘后报价；旧价、无价、缺状态一律不能当停牌或完整行情。"""
    from app.services.ml_r1_actions import return_evidence
    now = datetime.now(TZ)
    if now.date().isoformat() != day or now.hour < 15:
        raise ValueError('update_eod requires the current post-close session')
    snap = await eastmoney.market_snapshot()
    if snap.get('trade_date') != day or not snap.get('complete'):
        raise ValueError('EOD snapshot date mismatch or incomplete')
    prev_map = store.kline_latest_before(day)
    from app.services.ml_r1_service import calendar_days
    calendar = calendar_days()
    prior = calendar[calendar.index(day) - 1] if day in calendar and calendar.index(day) > 0 else None
    rows, missing, stamps = [], [], []
    for s in snap.get('stocks', []):
        code = s['code']
        stamp = _num(s.get('source_timestamp'))
        as_of = datetime.fromtimestamp(stamp, TZ).isoformat() if stamp else None
        close, prev = _num(s.get('price')), _num(s.get('prev_close'))
        amount, volume = _num(s.get('amount')), _num(s.get('volume'))
        time_ok = ml_r1.verified_close_time(as_of, day)
        trading = time_ok and volume is not None and volume > 0 and close is not None and close > 0
        if not trading or prev is None or prev <= 0:
            missing.append(code)
            continue
        p = prev_map.get(code) or {}
        action_record = dict(store.kv_get('corp_actions:' + code) or {})
        action_record['previous_trade_day_verified'] = bool(prior and prior == p.get('trade_date'))
        action = return_evidence(previous_close=p.get('close'), close=close,
                                 official_reference=prev, previous_day=p.get('trade_date'),
                                 day=day, record=action_record)
        meta = {'source_as_of': as_of, 'eod_verified': True,
                'official_reference_verified': True, 'raw_prev_close': p.get('close'),
                'return_basis': action.get('basis'), 'action_reason': action.get('reason'),
                'security_state': 'trading', 'state_verified': True, 'state_date': day,
                'state_source': 'eastmoney_clist_positive_session_volume'}
        # 规则由带有效期的证据记录提供；不从股票名称/代码推断日期制度。
        rule = store.kv_get('ml_r1:limit_rule:' + code + ':' + day)
        if rule:
            meta['limit_rule'] = rule
        else:
            evidence = store.kv_get('ml_r1:security_rule_evidence:' + code + ':' + day)
            if evidence:
                meta['security_rule_evidence'] = evidence
        rows.append({'trade_date': day, 'stock_code': code, 'close': close,
                     'prev_close': prev, 'open': _num(s.get('open')),
                     'high': _num(s.get('high')), 'low': _num(s.get('low')),
                     'volume': volume, 'amount': amount, 'tr_factor': action.get('factor'),
                     'action_day': bool(action.get('action_day')),
                     'source': 'eastmoney_snapshot_eod', 'input_meta': meta})
        stamps.append(as_of)
    count = store.kline_daily_save(rows)
    univ = _save_universe_record(snap)
    run = {'day': day, 'source': snap.get('source'), 'complete': not missing,
           'source_as_of': min(stamps) if stamps else None,
           'source_as_of_max': max(stamps) if stamps else None,
           'collected_at': now.isoformat(), 'priced_rows': count, 'total': snap.get('total'),
           'missing_codes': missing, 'status': 'final' if not missing else 'partial_preview',
           'formula_version': ml_r1.VERSION}
    store.kv_append_snapshot('ml_r1:eod_quote_run:' + day, run)
    store.kv_set('ml_r1:eod_quote_run:' + day, run)
    return {**run, 'kline_rows': count, 'universe': univ}


async def collect_all_members(codes: list[str], concurrency: int = 6) -> dict:
    """全部板块成员点时采集（免费源 boards.members）。失败板块如实报告，不重试到死。"""
    from app.services import free_hotspots
    sem = asyncio.Semaphore(concurrency)
    saved, errors = 0, []

    async def one(code: str) -> None:
        nonlocal saved
        async with sem:
            try:
                await free_hotspots.collect_members(code)
                saved += 1
            except Exception as exc:  # noqa: BLE001 - 单板块失败不拖垮整批
                errors.append({'code': code, 'error': type(exc).__name__})

    await asyncio.gather(*(one(c) for c in codes))
    return {'requested': len(codes), 'saved': saved, 'errors': errors[:50],
            'error_count': len(errors),
            'collected_at': datetime.now(TZ).isoformat()}


def board_codes_for(day: str) -> dict[str, list[str]]:
    """当日完整板块快照目录（board_type 2=行业 3=概念），run 元数据 complete 才算。"""
    out: dict[str, list[str]] = {}
    for plate_type, board_type in (('14', 2), ('15', 3)):
        rows, runs = store.board_snapshot_range(board_type, day, day)
        complete = any(bool(r.get('complete')) and r.get('trade_date') == day for r in runs)
        seen: dict[str, str] = {}
        for r in rows:
            seen.setdefault(str(r.get('plate_code')), r.get('plate_name', ''))
        out[plate_type] = sorted(seen) if complete else []
        out[plate_type + '_names'] = seen  # type: ignore[assignment]
    return out
