"""数据发布新鲜度监控。

按数据域定义交易日内的预期发布时限：盘中任务看分钟级容忍度，
收盘任务看当日截止时刻。评估只读已发布快照表与交易日历 kv，
不访问任何上游数据源，因此评估本身永远不会拖慢采集。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from app.core.store import store, Store

TZ = ZoneInfo('Asia/Shanghai')
logger = logging.getLogger('zzquant.freshness')


@dataclass(frozen=True)
class DomainRule:
    """一个数据域的新鲜度规则。

    mode='intraday': 交易时段内必须有当日数据；minute_samples 额外看分钟差。
    mode='close':    当日交易截止时间 deadline 之前表里必须已有当日 trade_date。
    """
    name: str
    table: str
    time_col: str
    mode: str            # 'intraday' | 'close'
    intraday_tol: int = 10   # 分钟
    deadline: str = '18:00'  # close 模式当日截止时刻


RULES: tuple[DomainRule, ...] = (
    # 盘中分钟级域（9:15-15:10 评估）
    DomainRule('popular', 'popular_snapshots', 'trade_date', 'intraday', intraday_tol=15),
    DomainRule('limit_pools', 'limit_pool_snapshots', 'trade_date', 'intraday', intraday_tol=15),
    DomainRule('index_trends', 'index_snapshots', 'trade_date', 'intraday', intraday_tol=15),
    DomainRule('boards', 'board_snapshots', 'trade_date', 'intraday', intraday_tol=15),
    DomainRule('market_minute', 'minute_samples', 'date', 'intraday', intraday_tol=10),
    # 收盘后截止域（15:10 后评估）
    DomainRule('topic_close', 'topic_snapshots', 'trade_date', 'close', deadline='15:35'),
    DomainRule('daily_close', 'daily_close_runs', 'trade_date', 'close', deadline='15:40'),
    DomainRule('lhb', 'lhb_snapshots', 'trade_date', 'close', deadline='17:00'),
    DomainRule('block_top', 'block_top_snapshots', 'trade_date', 'close', deadline='18:00'),
    DomainRule('movement', 'movement_snapshots', 'trade_date', 'close', deadline='18:00'),
    DomainRule('index_history', 'index_history_snapshots', 'trade_date', 'close', deadline='18:00'),
    DomainRule('auction_close', 'auction_daily', 'trade_date', 'close', deadline='18:00'),
    DomainRule('ths_board_daily', 'ths_board_daily', 'trade_date', 'close', deadline='18:30'),
)


def _hhmm(s: str) -> dtime:
    h, m = s.split(':')
    return dtime(int(h), int(m))


def _latest_marker(db: Store, rule: DomainRule) -> tuple[str, str] | None:
    """返回 (trade_date, minute_or_empty) —— 每域取最新一条的时间标记。"""
    with db._conn() as conn:  # noqa: SLF001 - 同模块层访问约定
        if rule.table == 'minute_samples':
            row = conn.execute(
                "SELECT date, minute FROM minute_samples ORDER BY replace(date, '-', '') DESC, minute DESC LIMIT 1"
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT {rule.time_col} AS d FROM {rule.table} ORDER BY replace({rule.time_col}, '-', '') DESC LIMIT 1"
            ).fetchone()
    if row is None:
        return None
    raw_date = str(row['date'] if rule.table == 'minute_samples' else row['d'])
    try:
        day = datetime.strptime(raw_date.replace('-', ''), '%Y%m%d').date().isoformat()
    except ValueError:
        return None
    return day, str(row['minute'] or '') if rule.table == 'minute_samples' else ''



def evaluate(db: Store = store, now: datetime | None = None) -> dict:
    """实时评估全部数据域，返回各域状态。非交易时段返回 not_in_session。"""
    now = now or datetime.now(TZ)
    t = now.time()
    # 周末没有 A 股盘中/盘后发布任务。先在时钟窗口判断前排除，
    # 否则周末 09:15-15:10 会被误当成盘中，并在日历尚未刷新时产生
    # 全域 pending 的伪状态。
    if now.weekday() >= 5:
        return {'evaluated_at': now.isoformat(), 'session': 'not_in_session', 'domains': {}, 'calendar_ok': True}
    in_market = dtime(9, 15) <= t <= dtime(15, 10)
    after_close = dtime(15, 10) < t <= dtime(23, 59)
    if not (in_market or after_close):
        return {'evaluated_at': now.isoformat(), 'session': 'not_in_session', 'domains': {}, 'calendar_ok': True}

    cal = db.kv_get('collector_calendar_v1', {}) or {}
    days = cal.get('days') or []
    # 当天不在交易日历（节假日/周末/日历未确认）时不判 stale，避免误报
    day = now.date().isoformat()
    calendar_ok = day in days

    domains: dict[str, dict] = {}
    for rule in RULES:
        marker = _latest_marker(db, rule)
        if marker is None:
            status, detail = 'missing', 'no rows in table'
        else:
            latest_day, latest_minute = marker
            if rule.mode == 'intraday':
                if not in_market:
                    status, detail = 'ok', 'after close'
                elif latest_day != day:
                    status, detail = 'stale', f'latest {latest_day} not today'
                elif rule.table == 'minute_samples' and latest_minute:
                    try:
                        lh, lm = int(latest_minute[:2]), int(latest_minute[3:5])
                        age = (now.hour * 60 + now.minute) - (lh * 60 + lm)
                        if age > rule.intraday_tol:
                            status, detail = 'stale', f'latest minute {latest_minute}, age {age}m'
                        else:
                            status, detail = 'ok', f'age {age}m'
                    except ValueError:
                        status, detail = 'ok', latest_minute
                else:
                    status, detail = 'ok', 'today'
            else:  # close 模式
                deadline = _hhmm(rule.deadline)
                past_deadline = t >= deadline
                if latest_day == day:
                    status, detail = 'ok', 'today'
                elif past_deadline:
                    status, detail = 'stale', f'latest {latest_day}, deadline {rule.deadline} passed'
                else:
                    status, detail = 'pending', f'deadline {rule.deadline}'

        if not calendar_ok and status == 'stale':
            # 日历未确认时不产生 stale 误报
            status, detail = 'pending', 'calendar unconfirmed'
        domains[rule.name] = {
            'status': status, 'detail': detail,
            'latest_day': marker[0] if marker else None,
            'mode': rule.mode,
        }

    stale = [n for n, d in domains.items() if d['status'] == 'stale']
    missing = [n for n, d in domains.items() if d['status'] == 'missing']
    return {
        'evaluated_at': now.isoformat(),
        'session': 'intraday' if in_market else 'after_close',
        'calendar_ok': calendar_ok,
        'domains': domains,
        'stale_domains': stale,
        'missing_domains': missing,
    }


def run_check(db: Store = store) -> dict:
    """worker 心跳周期调用：评估 + 状态变化落 kv + 变化时告警日志。"""
    result = evaluate(db)
    if result.get('session') == 'not_in_session':
        return result
    prev = db.kv_get('collector_freshness_v1', {}) or {}
    prev_states = {n: d.get('status') for n, d in (prev.get('domains') or {}).items()}
    cur_states = {n: d.get('status') for n, d in result['domains'].items()}
    if cur_states != prev_states:
        for name, st in cur_states.items():
            old = prev_states.get(name)
            if old != st and st in ('stale', 'missing'):
                logger.error('DATA FRESHNESS ALERT: %s -> %s (%s)', name, st, result['domains'][name]['detail'])
            elif old != st and st == 'ok' and old in ('stale', 'missing'):
                logger.info('data freshness recovered: %s -> ok', name)
        result['changed_at'] = result['evaluated_at']
        db.kv_set('collector_freshness_v1', result)
    return result
