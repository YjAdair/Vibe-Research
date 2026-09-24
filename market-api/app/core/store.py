"""SQLite 持久化：题材表格、用户订阅、用户信息等。

行情类数据走缓存/实时计算，只有需要落库的业务对象（题材、订阅）才写盘。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from app.config import settings


class Store:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or settings.db_path
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            # WAL：采集进程分钟级写入与 API 读完全并发，读不再被写事务阻塞
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS topics (
                    id INTEGER,
                    unique_key TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    content TEXT DEFAULT '',
                    rows_json TEXT DEFAULT '[]',
                    score REAL DEFAULT 0,
                    today_pct REAL DEFAULT 0,
                    up_count INTEGER DEFAULT 0,
                    stock_count INTEGER DEFAULT 0,
                    up_ratio REAL DEFAULT 0,
                    leader_count INTEGER DEFAULT 0,
                    limit_up_count INTEGER DEFAULT 0,
                    limit_down_count INTEGER DEFAULT 0,
                    down_count INTEGER,
                    reasons_json TEXT DEFAULT '[]',
                    selection_scope TEXT DEFAULT 'all',
                    is_top INTEGER DEFAULT 0,
                    is_deleted INTEGER DEFAULT 0,
                    created_time TEXT,
                    updated_time TEXT
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    user_id TEXT,
                    module_code TEXT,
                    expires_at TEXT,
                    PRIMARY KEY (user_id, module_code)
                );
                CREATE TABLE IF NOT EXISTS module_usage_daily (
                    user_id TEXT NOT NULL,
                    module_code TEXT NOT NULL,
                    day TEXT NOT NULL,
                    used_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, module_code, day)
                );
                CREATE TABLE IF NOT EXISTS topic_follows (
                    user_id TEXT NOT NULL,
                    unique_key TEXT NOT NULL,
                    created_time TEXT,
                    PRIMARY KEY (user_id, unique_key)
                );
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS ai_reports (
                    id TEXT PRIMARY KEY,
                    report_type TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(report_type, trade_date)
                );
                CREATE TABLE IF NOT EXISTS minute_samples (
                    date TEXT NOT NULL,
                    minute TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (date, minute)
                );
                CREATE TABLE IF NOT EXISTS topic_snapshots (
                    trade_date TEXT NOT NULL,
                    unique_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (trade_date, unique_key)
                );
                CREATE TABLE IF NOT EXISTS daily_close (
                    trade_date TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT,
                    market_type TEXT,
                    concept TEXT,
                    close REAL,
                    prev_close REAL,
                    PRIMARY KEY (trade_date, stock_code)
                );
                CREATE TABLE IF NOT EXISTS daily_close_runs (
                    trade_date TEXT PRIMARY KEY,
                    metadata TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS limit_pool_snapshots (
                    pool TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (pool, trade_date)
                );
                CREATE TABLE IF NOT EXISTS index_snapshots (
                    trade_date TEXT NOT NULL,
                    slot TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (trade_date, slot)
                );
                CREATE TABLE IF NOT EXISTS lhb_snapshots (
                    trade_date TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lhb_seats (
                    trade_date TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    side INTEGER NOT NULL,
                    dept_code TEXT NOT NULL,
                    dept_name TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    buy_amount REAL,
                    sell_amount REAL,
                    net_amount REAL,
                    rank INTEGER,
                    win_rate_3d REAL,
                    PRIMARY KEY (trade_date, stock_code, side, dept_code)
                );
                CREATE INDEX IF NOT EXISTS idx_lhb_seats_dept ON lhb_seats(dept_code, trade_date);
                CREATE INDEX IF NOT EXISTS idx_daily_close_code ON daily_close(stock_code, trade_date);
                CREATE TABLE IF NOT EXISTS board_snapshots (
                    trade_date TEXT NOT NULL,
                    board_type INTEGER NOT NULL,
                    plate_code TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (board_type, trade_date, plate_code)
                );
                CREATE INDEX IF NOT EXISTS idx_board_history
                    ON board_snapshots(board_type, plate_code, trade_date);
                CREATE TABLE IF NOT EXISTS board_snapshot_runs (
                    board_type INTEGER NOT NULL,
                    trade_date TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    PRIMARY KEY (board_type, trade_date)
                );
                CREATE TABLE IF NOT EXISTS board_daily_history (
                    board_type INTEGER NOT NULL,
                    trade_date TEXT NOT NULL,
                    plate_code TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (board_type, trade_date, plate_code)
                );
                CREATE INDEX IF NOT EXISTS idx_board_daily_code
                    ON board_daily_history(board_type, plate_code, trade_date);
                CREATE TABLE IF NOT EXISTS stock_flow_daily (
                    trade_date TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT,
                    pct REAL,
                    price REAL,
                    main_net_inflow REAL,
                    source_as_of TEXT,
                    PRIMARY KEY (trade_date, stock_code)
                );
                CREATE INDEX IF NOT EXISTS idx_stock_flow_code ON stock_flow_daily(stock_code, trade_date);
                CREATE TABLE IF NOT EXISTS plate_rank_daily (
                    plate_type INTEGER NOT NULL,
                    trade_date TEXT NOT NULL,
                    plate_code TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (plate_type, trade_date, plate_code)
                );
                CREATE INDEX IF NOT EXISTS idx_plate_rank_date ON plate_rank_daily(plate_type, trade_date);
                CREATE TABLE IF NOT EXISTS plate_rank_day_meta (
                    plate_type INTEGER NOT NULL,
                    trade_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT,
                    collected_at TEXT,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    fingerprint TEXT,
                    PRIMARY KEY (plate_type, trade_date)
                );
                CREATE TABLE IF NOT EXISTS plate_kline_origin (
                    kind TEXT NOT NULL,
                    plate_code TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    last_date TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL,
                    PRIMARY KEY (kind, plate_code)
                );
                CREATE TABLE IF NOT EXISTS plate_reason_daily (
                    plate_code TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    msg_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (plate_code, trade_date, msg_id)
                );
                CREATE INDEX IF NOT EXISTS idx_plate_reason_date ON plate_reason_daily(trade_date);
                CREATE TABLE IF NOT EXISTS ths_board_daily (
                    board_code TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (board_code, trade_date)
                );
                CREATE INDEX IF NOT EXISTS idx_ths_board_daily_date
                    ON ths_board_daily(trade_date);
                CREATE TABLE IF NOT EXISTS auction_daily (
                    trade_date TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (trade_date, stock_code)
                );
                CREATE INDEX IF NOT EXISTS idx_auction_code ON auction_daily(stock_code, trade_date);
                CREATE TABLE IF NOT EXISTS auction_evidence (
                    trade_date TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    source_as_of TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (trade_date, stock_code, source_as_of, digest)
                );
                CREATE TABLE IF NOT EXISTS stock_intraday_sessions (
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    source_as_of TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (symbol, trade_date)
                );
                CREATE TABLE IF NOT EXISTS collector_runs (
                    job TEXT NOT NULL,
                    slot TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    detail TEXT,
                    PRIMARY KEY (job, slot)
                );
                CREATE TABLE IF NOT EXISTS index_history_snapshots (
                    trade_date TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS movement_snapshots (
                    trade_date TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS popular_snapshots (
                    trade_date TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS popular_snapshots_minutes (
                    trade_date TEXT NOT NULL,
                    minute TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (trade_date, minute)
                );
                CREATE TABLE IF NOT EXISTS em_popular_rank_hist (
                    stock_code TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    PRIMARY KEY (stock_code, trade_date)
                );
                CREATE INDEX IF NOT EXISTS idx_em_popular_rank_day
                    ON em_popular_rank_hist(trade_date, stock_code);
                CREATE TABLE IF NOT EXISTS em_popular_rank_cover (
                    stock_code TEXT PRIMARY KEY,
                    from_date TEXT NOT NULL,
                    to_date TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS block_top_snapshots (
                    trade_date TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS king_pool_snapshots (
                    trade_date TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_uplimit_snapshots (
                    trade_date TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plate_popular_snapshots (
                    plate_type INTEGER NOT NULL,
                    trade_date TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (plate_type, trade_date)
                );
                CREATE TABLE IF NOT EXISTS plate_members_snapshots (
                    plate_type INTEGER NOT NULL,
                    trade_date TEXT NOT NULL,
                    plate_code TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (plate_type, trade_date, plate_code)
                );
                CREATE TABLE IF NOT EXISTS ml_target_catalog (
                    taxonomy_id INTEGER NOT NULL,
                    target_code TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    parent_taxonomy_id INTEGER,
                    parent_code TEXT,
                    observed_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    verification_status TEXT NOT NULL,
                    notes TEXT DEFAULT '',
                    PRIMARY KEY (taxonomy_id, target_code)
                );
                CREATE TABLE IF NOT EXISTS ml_vendor_mapping (
                    taxonomy_id INTEGER NOT NULL,
                    target_code TEXT NOT NULL,
                    vendor TEXT NOT NULL,
                    vendor_code TEXT,
                    verification_status TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    notes TEXT DEFAULT '',
                    PRIMARY KEY (taxonomy_id, target_code, vendor)
                );
                CREATE TABLE IF NOT EXISTS ml_membership_validity (
                    taxonomy_id INTEGER NOT NULL,
                    target_code TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    effective_from TEXT NOT NULL,
                    effective_to TEXT,
                    vendor TEXT,
                    verification_status TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    notes TEXT DEFAULT '',
                    PRIMARY KEY (taxonomy_id, target_code, stock_code, effective_from)
                );
                CREATE TABLE IF NOT EXISTS ml_r1_revisions (
                    digest TEXT PRIMARY KEY, plate_type INTEGER, taxonomy_version TEXT,
                    trade_date TEXT, formula_version TEXT, payload TEXT, saved_at TEXT);
                CREATE TABLE IF NOT EXISTS ml_r1_inputs (
                    digest TEXT PRIMARY KEY, payload TEXT NOT NULL, saved_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ml_r1_snapshots (
                    formula_id TEXT NOT NULL,
                    plate_type INTEGER NOT NULL,
                    taxonomy_version TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    saved_at TEXT NOT NULL,
                    PRIMARY KEY(formula_id, plate_type, taxonomy_version, trade_date)
                );
                CREATE TABLE IF NOT EXISTS stock_kline_daily (
                    trade_date TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    open REAL, high REAL, low REAL, close REAL,
                    prev_close REAL,
                    volume REAL, amount REAL,
                    tr_factor REAL,
                    action_day INTEGER NOT NULL DEFAULT 0,
                    close_hfq REAL,
                    source TEXT, updated_at TEXT,
                    PRIMARY KEY (trade_date, stock_code)
                );
                CREATE INDEX IF NOT EXISTS idx_stock_kline_code
                    ON stock_kline_daily(stock_code, trade_date);
                """
            )
            ml_cols = {r[1] for r in conn.execute('PRAGMA table_info(ml_r1_snapshots)')}
            kline_cols = {r[1] for r in conn.execute('PRAGMA table_info(stock_kline_daily)')}
            if 'input_meta' not in kline_cols:
                conn.execute('ALTER TABLE stock_kline_daily ADD COLUMN input_meta TEXT')
            if ml_cols and 'plate_type' not in ml_cols:
                conn.execute('ALTER TABLE ml_r1_snapshots RENAME TO ml_r1_snapshots_legacy')
                conn.execute('''CREATE TABLE ml_r1_snapshots (
                    formula_id TEXT NOT NULL, plate_type INTEGER NOT NULL,
                    taxonomy_version TEXT NOT NULL, trade_date TEXT NOT NULL,
                    payload TEXT NOT NULL, saved_at TEXT NOT NULL,
                    PRIMARY KEY(formula_id, plate_type, taxonomy_version, trade_date))''')
            catalog_cols = {r[1] for r in conn.execute('PRAGMA table_info(ml_target_catalog)')}
            if 'parent_taxonomy_id' not in catalog_cols:
                conn.execute('ALTER TABLE ml_target_catalog ADD COLUMN parent_taxonomy_id INTEGER')
            if 'parent_code' not in catalog_cols:
                conn.execute('ALTER TABLE ml_target_catalog ADD COLUMN parent_code TEXT')
            now = datetime.now().isoformat(timespec='seconds')
            conn.executemany(
                'INSERT OR IGNORE INTO ml_target_catalog '
                '(taxonomy_id, target_code, target_name, parent_taxonomy_id, parent_code, '
                'observed_at, source, verification_status, notes) VALUES(?,?,?,?,?,?,?,?,?)',
                [(17, '801660', '通信', None, None, now, 'target_page_observation', 'unverified',
                  '仅记录目标页面观察到的目录项；不是东财映射。'),
                 (18, '801003', '5G', 17, '801660', now, 'target_page_observation', 'unverified',
                  '仅记录目标页面观察到的目录项；不是东财映射。')],
            )
            conn.executemany(
                'INSERT OR IGNORE INTO ml_vendor_mapping VALUES(?,?,?,?,?,?,?)',
                [(17, '801660', 'unverified', None, 'unverified', now,
                  '目标目录代码已观察；供应商及等价映射未验证。'),
                 (18, '801003', 'unverified', None, 'unverified', now,
                 '目标目录代码已观察；供应商及等价映射未验证。')],
            )
            conn.execute(
                'UPDATE ml_target_catalog SET parent_taxonomy_id=?, parent_code=? '
                'WHERE taxonomy_id=? AND target_code=?',
                (17, '801660', 18, '801003'),
            )
            cols = {r[1] for r in conn.execute('PRAGMA table_info(daily_close)')}
            for col, spec in [('high', 'REAL'), ('low', 'REAL'), ('open', 'REAL'), ('circulation_value', 'REAL'), ('turnover_ratio', 'REAL'), ('vol_ratio', 'REAL')]:
                if col not in cols:
                    conn.execute(f'ALTER TABLE daily_close ADD COLUMN {col} {spec}')
            tcols = {r[1] for r in conn.execute('PRAGMA table_info(topics)')}
            for col, spec in [('id', 'INTEGER'), ('down_count', 'INTEGER')]:
                if col not in tcols:
                    conn.execute(f'ALTER TABLE topics ADD COLUMN {col} {spec}')
            pcols = {r[1] for r in conn.execute('PRAGMA table_info(plate_rank_daily)')}
            if 'money_leader_exact' not in pcols:
                conn.execute('ALTER TABLE plate_rank_daily ADD COLUMN money_leader_exact REAL')
            conn.execute(
                'CREATE TABLE IF NOT EXISTS plate_rank_day_meta ('
                'plate_type INTEGER NOT NULL, trade_date TEXT NOT NULL, status TEXT NOT NULL, '
                'source TEXT, collected_at TEXT, row_count INTEGER NOT NULL DEFAULT 0, '
                'fingerprint TEXT, PRIMARY KEY (plate_type, trade_date))'
            )

    def board_snapshot_save(self, board_type: int, trade_date: str, rows: list[dict], metadata: dict) -> None:
        """Only complete, validated batches are published, in one transaction."""
        if not rows or len({r['plate_code'] for r in rows}) != len(rows):
            raise ValueError('Empty or duplicate board snapshot')
        with self._conn() as conn:
            conn.execute('DELETE FROM board_snapshots WHERE board_type=? AND trade_date=?', (board_type, trade_date))
            conn.executemany('INSERT INTO board_snapshots VALUES(?,?,?,?)', [
                (trade_date, board_type, r['plate_code'], json.dumps(r, ensure_ascii=False)) for r in rows
            ])
            conn.execute('INSERT OR REPLACE INTO board_snapshot_runs VALUES(?,?,?)',
                         (board_type, trade_date, json.dumps(metadata, ensure_ascii=False)))

    def board_snapshot_range(self, board_type: int, start: str, end: str) -> tuple[list[dict], list[dict]]:
        with self._conn() as conn:
            rows = conn.execute('SELECT payload FROM board_snapshots WHERE board_type=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date DESC, plate_code', (board_type, start, end)).fetchall()
            runs = conn.execute('SELECT metadata FROM board_snapshot_runs WHERE board_type=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date DESC', (board_type, start, end)).fetchall()
        return [json.loads(r['payload']) for r in rows], [json.loads(r['metadata']) for r in runs]

    def board_history_save(self, board_type: int, code: str, rows: list[dict]) -> None:
        """Upsert a validated symbol series without touching live snapshots."""
        with self._conn() as conn:
            merged = []
            for row in rows:
                previous = conn.execute('SELECT payload FROM board_daily_history WHERE board_type=? AND trade_date=? AND plate_code=?', (board_type, row['date1'], code)).fetchone()
                old = json.loads(previous['payload']) if previous else {}
                # A partially recovered provider must never erase a known value.
                merged.append({**old, **{k: v for k, v in row.items() if v is not None or k not in old}})
            conn.executemany('INSERT OR REPLACE INTO board_daily_history VALUES(?,?,?,?)', [
                (board_type, row['date1'], code, json.dumps(row, ensure_ascii=False)) for row in merged
            ])

    def board_history_range(self, board_type: int, start: str, end: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute('SELECT payload FROM board_daily_history WHERE board_type=? AND trade_date BETWEEN ? AND ?', (board_type, start, end)).fetchall()
        return [json.loads(row['payload']) for row in rows]

    def stock_flow_save(self, trade_date: str, rows: list[dict], metadata: dict | None = None) -> int:
        """Publish a full-market main-net-inflow daily snapshot (eastmoney push2delay)."""
        if not rows:
            raise ValueError('Empty stock flow batch')
        codes = [r.get('code') for r in rows]
        if any(not c for c in codes) or len(set(codes)) != len(codes):
            raise ValueError('Duplicate or missing codes in stock flow batch')
        metadata = dict(metadata or {})
        metadata.update(trade_date=trade_date, count=len(rows))
        with self._conn() as conn:
            conn.execute('DELETE FROM stock_flow_daily WHERE trade_date=?', (trade_date,))
            conn.executemany(
                'INSERT INTO stock_flow_daily VALUES(?,?,?,?,?,?,?)',
                [
                    (
                        trade_date,
                        r['code'],
                        r.get('name'),
                        r.get('pct'),
                        r.get('price'),
                        r.get('main_net_inflow'),
                        r.get('source_as_of'),
                    )
                    for r in rows
                ],
            )
            conn.execute(
                'INSERT OR REPLACE INTO kv VALUES(?,?)',
                ('stock_flow_run:' + trade_date, json.dumps(metadata, ensure_ascii=False)),
            )
        return len(rows)

    def stock_flow_range(self, start: str, end: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT trade_date, stock_code, stock_name, pct, price, main_net_inflow, source_as_of FROM stock_flow_daily WHERE trade_date BETWEEN ? AND ?',
                (start, end),
            ).fetchall()
        return [dict(r) for r in rows]

    def stock_flow_dates(self, limit: int = 60) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT DISTINCT trade_date FROM stock_flow_daily ORDER BY trade_date DESC LIMIT ?', (limit,)
            ).fetchall()
        return [r['trade_date'] for r in rows]

    def plate_rank_save(self, plate_type: int, trade_date: str, rows: list[dict]) -> int:
        """Publish a full plate daily ranking snapshot (one row per plate)."""
        if not rows:
            raise ValueError('Empty plate rank batch')
        with self._conn() as conn:
            conn.execute('DELETE FROM plate_rank_daily WHERE plate_type=? AND trade_date=?', (plate_type, trade_date))
            conn.executemany(
                'INSERT INTO plate_rank_daily(plate_type, trade_date, plate_code, payload) VALUES(?,?,?,?)',
                [
                    (plate_type, trade_date, r['plate_code'], json.dumps(r, ensure_ascii=False))
                    for r in rows
                    if r.get('plate_code')
                ],
            )
        return len(rows)

    def plate_rank_day_meta_save(self, plate_type: int, trade_date: str, *, status: str,
                                  source: str | None, collected_at: str | None,
                                  row_count: int, fingerprint: str | None) -> None:
        with self._conn() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO plate_rank_day_meta'
                '(plate_type, trade_date, status, source, collected_at, row_count, fingerprint) '
                'VALUES(?,?,?,?,?,?,?)',
                (plate_type, trade_date, status, source, collected_at, int(row_count), fingerprint),
            )

    def plate_rank_day_meta_get(self, plate_type: int, trade_date: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                'SELECT status, source, collected_at, row_count, fingerprint '
                'FROM plate_rank_day_meta WHERE plate_type=? AND trade_date=?',
                (plate_type, trade_date),
            ).fetchone()
        return dict(row) if row else None

    def plate_rank_day_count(self, plate_type: int, trade_date: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                'SELECT count(*) AS n FROM plate_rank_daily WHERE plate_type=? AND trade_date=?',
                (plate_type, trade_date),
            ).fetchone()
        return int(row['n'] if row else 0)

    def plate_rank_exact_save(self, plate_type: int, trade_date: str, exact: dict[str, float]) -> int:
        """回填全精度 money_leader（原站 rank/days 的 sum_leader_money）。

        只 UPDATE 已存在的快照行；payload 中 money_leader 保持原站 /rank 的
        舍入值不变，保证本地 /rank 路由字段口径与原站一致。返回实际更新行数。
        """
        if not exact:
            return 0
        with self._conn() as conn:
            cur = conn.executemany(
                'UPDATE plate_rank_daily SET money_leader_exact=? '
                'WHERE plate_type=? AND trade_date=? AND plate_code=?',
                [(float(v), plate_type, trade_date, k) for k, v in exact.items()],
            )
        return cur.rowcount or 0

    def plate_rank_range(self, plate_type: int, start: str, end: str, with_exact: bool = False) -> list[dict]:
        """快照行（payload 反序列化）。with_exact=True 时附加 money_leader_exact
        列（仅供 rank/days 聚合内部使用，勿透传给 /rank 等原样响应接口）。"""
        cols = 'payload, money_leader_exact' if with_exact else 'payload'
        with self._conn() as conn:
            rows = conn.execute(
                f'SELECT {cols} FROM plate_rank_daily WHERE plate_type=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date DESC',
                (plate_type, start, end),
            ).fetchall()
        out = []
        for r in rows:
            d = json.loads(r['payload'])
            if with_exact and r['money_leader_exact'] is not None:
                d['money_leader_exact'] = r['money_leader_exact']
            out.append(d)
        return out

    def plate_rank_exact_pending(self, plate_type: int, limit: int = 400) -> list[str]:
        """待补 money_leader_exact 的快照日期（存在 NULL 行且无 done 标记），新→旧。"""
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT DISTINCT trade_date FROM plate_rank_daily '
                'WHERE plate_type=? AND money_leader_exact IS NULL ORDER BY trade_date DESC LIMIT ?',
                (plate_type, limit),
            ).fetchall()
        dates = [r['trade_date'] for r in rows]
        return [d for d in dates if not self.kv_get(f'plate_rank_exact_done:{plate_type}:{d}')]

    def plate_rank_dates(self, plate_type: int, limit: int = 60) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT DISTINCT trade_date FROM plate_rank_daily WHERE plate_type=? ORDER BY trade_date DESC LIMIT ?',
                (plate_type, limit),
            ).fetchall()
        return [r['trade_date'] for r in rows]

    def plate_kline_origin_save(self, kind: str, plate_code: str, series: dict) -> None:
        """保存原站校准层板块日K序列（kind: main=/v3/open/kline/d, sub=/kline/sub-plate）。

        商业化红线：数据来自原站公开接口，仅供开发期校准；上线前需替换为
        THS iFinD 等自有数据链路。序列整体替换（原站仅回传近端窗口）。
        """
        xs = series.get('x') or []
        payload = json.dumps({k: series.get(k) for k in ('x', 'y', 'vol', 'turnover')}, ensure_ascii=False)
        with self._conn() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO plate_kline_origin VALUES(?,?,?,?,?)',
                (kind, str(plate_code), datetime.now().isoformat(timespec='seconds'),
                 str(xs[-1]) if xs else '', payload),
            )

    def plate_kline_origin_get(self, kind: str, plate_code: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                'SELECT fetched_at, last_date, payload FROM plate_kline_origin WHERE kind=? AND plate_code=?',
                (kind, str(plate_code)),
            ).fetchone()
        if not row:
            return None
        data = json.loads(row['payload'])
        data['fetched_at'] = row['fetched_at']
        data['last_date'] = row['last_date']
        return data

    def plate_reason_save(self, rows: list[dict]) -> int:
        """Upsert plate popularity reason messages keyed by (plate_code, date, msg_id)."""
        if not rows:
            return 0
        with self._conn() as conn:
            conn.executemany(
                'INSERT OR REPLACE INTO plate_reason_daily VALUES(?,?,?,?)',
                [
                    (r['plate_code'], r.get('date') or '', str(r.get('msg_id') or r.get('newid') or r.get('id') or ''), json.dumps(r, ensure_ascii=False))
                    for r in rows
                    if r.get('plate_code') and (r.get('msg_id') or r.get('newid') or r.get('id'))
                ],
            )
        return len(rows)

    def plate_reason_list(self, plate_code: str, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT payload FROM plate_reason_daily WHERE plate_code=? ORDER BY trade_date DESC, msg_id DESC LIMIT ?',
                (plate_code, limit),
            ).fetchall()
        return [json.loads(r['payload']) for r in rows]

    def plate_reason_get(self, msg_id: str) -> dict | None:
        """按 msg_id 取驱动消息（原站 reason/content?msgid= 数据源）。"""
        with self._conn() as conn:
            row = conn.execute(
                'SELECT payload FROM plate_reason_daily WHERE msg_id=? ORDER BY trade_date DESC LIMIT 1',
                (str(msg_id),),
            ).fetchone()
        return json.loads(row['payload']) if row else None

    def ths_board_daily_save(self, board_code: str, bars: list[dict]) -> int:
        """Upsert THS board bars (independent vendor series, never merged with eastmoney)."""
        if not bars:
            return 0
        with self._conn() as conn:
            conn.executemany(
                'INSERT OR REPLACE INTO ths_board_daily VALUES(?,?,?)',
                [(board_code, b['date'], json.dumps(b, ensure_ascii=False)) for b in bars])
        return len(bars)

    def ths_board_daily_range(self, board_code: str, start: str, end: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT payload FROM ths_board_daily WHERE board_code=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date',
                (board_code, start, end)).fetchall()
        return [json.loads(row['payload']) for row in rows]

    def ths_board_daily_stats(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT board_code, count(*) AS bars, max(trade_date) AS latest FROM ths_board_daily GROUP BY board_code').fetchall()
        return [dict(r) for r in rows]

    def auction_save(self, rows: list[dict], evidence: list[dict]) -> None:
        """Publish values and their raw evidence atomically; missing never erases known."""
        import hashlib
        with self._conn() as conn:
            for row in rows:
                old = conn.execute('SELECT payload FROM auction_daily WHERE trade_date=? AND stock_code=?', (row['date'], row['stock_code'])).fetchone()
                previous = json.loads(old['payload']) if old else {}
                sources = {**previous.get('field_sources', {}), **row.get('field_sources', {})}
                merged = {**previous, **{k: v for k, v in row.items() if v is not None or k not in previous}, 'field_sources': sources}
                conn.execute('INSERT OR REPLACE INTO auction_daily VALUES(?,?,?)', (row['date'], row['stock_code'], json.dumps(merged, ensure_ascii=False)))
            for item in evidence:
                raw = json.dumps(item, ensure_ascii=False, sort_keys=True)
                digest = hashlib.sha256(json.dumps(item['quote'], sort_keys=True).encode()).hexdigest()
                conn.execute('INSERT OR IGNORE INTO auction_evidence VALUES(?,?,?,?,?)',
                             (item['date'], item['quote']['code'], item['quote']['timestamp'], digest, raw))

    def auction_range(self, start: str, end: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute('SELECT payload FROM auction_daily WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date DESC, stock_code', (start, end)).fetchall()
        return [json.loads(row['payload']) for row in rows]

    def auction_evidence_range(self, start: str, end: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute('SELECT trade_date, stock_code, source_as_of, payload FROM auction_evidence WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date, stock_code, source_as_of', (start, end)).fetchall()
        return [json.loads(row['payload']) for row in rows]

    def stock_intraday_save(self, symbol: str, sessions: list[dict]) -> None:
        with self._conn() as conn:
            for session in sessions:
                conn.execute('''INSERT INTO stock_intraday_sessions VALUES(?,?,?,?)
                    ON CONFLICT(symbol,trade_date) DO UPDATE SET source_as_of=excluded.source_as_of,payload=excluded.payload
                    WHERE excluded.source_as_of >= stock_intraday_sessions.source_as_of''',
                    (symbol, session['date'], session['source_as_of'], json.dumps(session,ensure_ascii=False)))

    def stock_intraday_get(self, symbol: str, date: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute('SELECT payload FROM stock_intraday_sessions WHERE symbol=? AND trade_date=?',(symbol,date)).fetchone()
        return json.loads(row['payload']) if row else None

    def stock_intraday_dates(self, symbol: str) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute('SELECT trade_date FROM stock_intraday_sessions WHERE symbol=? ORDER BY trade_date DESC LIMIT 20',(symbol,)).fetchall()
        return [row['trade_date'] for row in rows]

    def module_usage_get(self, user_id: str, module_code: str, day: str) -> int:
        """当日模块已用次数（原站免费试用计数，如 pct_interval_vip 每日 3 次）。"""
        with self._conn() as conn:
            row = conn.execute('SELECT used_count FROM module_usage_daily WHERE user_id=? AND module_code=? AND day=?',(user_id,module_code,day)).fetchone()
        return int(row['used_count']) if row else 0

    def module_usage_increment(self, user_id: str, module_code: str, day: str) -> int:
        """计数 +1 并返回新值；INSERT OR IGNORE 保证行存在。"""
        with self._conn() as conn:
            conn.execute('INSERT OR IGNORE INTO module_usage_daily(user_id,module_code,day,used_count) VALUES(?,?,?,0)',(user_id,module_code,day))
            conn.execute('UPDATE module_usage_daily SET used_count=used_count+1 WHERE user_id=? AND module_code=? AND day=?',(user_id,module_code,day))
            row = conn.execute('SELECT used_count FROM module_usage_daily WHERE user_id=? AND module_code=? AND day=?',(user_id,module_code,day)).fetchone()
        return int(row['used_count']) if row else 0

    def collector_run(self, job: str, slot: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute('SELECT * FROM collector_runs WHERE job=? AND slot=?',(job,slot)).fetchone()
        return dict(row) if row else None

    def collector_start(self, job: str, slot: str, owner: str, at: float) -> None:
        with self._conn() as conn:
            conn.execute('''INSERT INTO collector_runs(job,slot,owner,status,started_at) VALUES(?,?,?,'running',?)
                ON CONFLICT(job,slot) DO UPDATE SET owner=excluded.owner,status='running',started_at=excluded.started_at,
                finished_at=NULL,attempts=collector_runs.attempts+1,detail=NULL''',(job,slot,owner,at))

    def collector_finish(self, job: str, slot: str, owner: str, status: str, at: float, detail: dict) -> None:
        with self._conn() as conn:
            conn.execute('UPDATE collector_runs SET status=?,finished_at=?,detail=? WHERE job=? AND slot=? AND owner=?',
                         (status,at,json.dumps(detail,ensure_ascii=False),job,slot,owner))

    def collector_recent(self, limit: int = 30) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute('SELECT * FROM collector_runs ORDER BY started_at DESC LIMIT ?',(limit,)).fetchall()
        return [{**dict(row),'detail':json.loads(row['detail']) if row['detail'] else None} for row in rows]

    # ---- 通用 KV ----
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def kv_append_snapshot(self, prefix: str, record: dict) -> str:
        """原子追加不可变快照及索引；不裁剪历史，不把行情时点冒充可知时间。"""
        import hashlib
        stamp = record.get('collected_at')
        if not stamp or datetime.fromisoformat(stamp).tzinfo is None:
            raise ValueError('snapshot requires timezone-aware collected_at')
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False)
        suffix = stamp + ':' + hashlib.sha256(encoded.encode()).hexdigest()[:16]
        key = prefix + ':' + suffix
        with self._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT value FROM kv WHERE key=?', (prefix + ':index',)).fetchone()
            index = json.loads(old['value']) if old else []
            conn.execute('INSERT OR IGNORE INTO kv VALUES(?,?)', (key, encoded))
            conn.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',
                         (prefix + ':index', json.dumps(sorted(set(index) | {key}))))
        return key

    def kline_daily_save(self, rows: list[dict]) -> int:
        """批量 upsert stock_kline_daily。rows 必须含 trade_date/stock_code。"""
        if not rows:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO stock_kline_daily(trade_date, stock_code, open, high, low, "
                "close, prev_close, volume, amount, tr_factor, action_day, close_hfq, source, "
                "updated_at, input_meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(r["trade_date"], r["stock_code"], r.get("open"), r.get("high"), r.get("low"),
                  r.get("close"), r.get("prev_close"), r.get("volume"), r.get("amount"),
                  r.get("tr_factor"), 1 if r.get("action_day") else 0, r.get("close_hfq"),
                  r.get("source"), now, json.dumps(r.get('input_meta') or {}, ensure_ascii=False)) for r in rows])
        return len(rows)

    def kline_fill_missing(self, rows: list[dict]) -> dict:
        """独立历史来源补空；OHLC或已验证状态冲突时隔离整行，保留原始证据供复核。"""
        import math
        filled, states, inserted, conflicts = 0, 0, 0, []
        fields = ('open', 'high', 'low', 'close', 'volume', 'amount')
        now = datetime.now().astimezone().isoformat()
        with self._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for row in rows:
                old = conn.execute('SELECT * FROM stock_kline_daily WHERE trade_date=? AND stock_code=?',
                                   (row['trade_date'],row['stock_code'])).fetchone()
                meta = json.loads(old['input_meta'] or '{}') if old else {}
                incoming = row.get('input_meta') or {}
                price_conflict = old and any(old[f] is not None and row.get(f) is not None and
                    not math.isclose(float(old[f]),float(row[f]),rel_tol=0,abs_tol=.011) for f in ('open','high','low','close'))
                quantity_conflict = old and any(old[f] is not None and row.get(f) is not None and not math.isclose(float(old[f]),float(row[f]),rel_tol=1e-6,abs_tol=1.0 if f == 'volume' else .01) for f in ('amount','volume'))
                merged_prices = {f:old[f] if old and old[f] is not None else row.get(f) for f in ('open','high','low','close')}
                invalid_merged_prices = all(v is not None for v in merged_prices.values()) and not (
                    0 < merged_prices['low'] <= min(merged_prices['open'],merged_prices['close']) <= max(merged_prices['open'],merged_prices['close']) <= merged_prices['high'])
                state_valid = (meta.get('state_verified') is True and meta.get('state_date') == row['trade_date']
                               and bool(meta.get('state_source')) and meta.get('security_state') in ('trading','suspended'))
                state_conflict = state_valid and incoming.get('state_verified') is True and meta.get('security_state') != incoming.get('security_state')
                if price_conflict or invalid_merged_prices or quantity_conflict or state_conflict:
                    conflicts.append({'stock_code':row['stock_code'],'date':row['trade_date'],
                                      'reason':'price_conflict' if price_conflict else 'merged_ohlc_conflict' if invalid_merged_prices else 'quantity_conflict' if quantity_conflict else 'state_conflict'})
                    continue
                if not old:
                    conn.execute('INSERT INTO stock_kline_daily(trade_date,stock_code,source,updated_at,input_meta) VALUES(?,?,?,?,?)',
                                 (row['trade_date'],row['stock_code'],row.get('source'),now,'{}'))
                    inserted += 1
                for field in fields:
                    if (not old or old[field] is None) and row.get(field) is not None:
                        conn.execute('UPDATE stock_kline_daily SET '+field+'=? WHERE trade_date=? AND stock_code=?',
                                     (row[field],row['trade_date'],row['stock_code']))
                        meta.setdefault('field_sources',{})[field] = incoming.get('history_evidence_key')
                        filled += 1
                if not state_valid and incoming.get('state_verified') is True:
                    meta.update({k:incoming[k] for k in ('state_verified','state_date','state_source','security_state')})
                    states += 1
                meta['history_evidence'] = {k:incoming.get(k) for k in ('history_source','history_evidence_key','history_collected_at','provider_preclose','provider_pct','provider_is_st','provider_trade_status','provider_volume_shares','volume_unit','amount_unit')}
                conn.execute('UPDATE stock_kline_daily SET input_meta=?,updated_at=? WHERE trade_date=? AND stock_code=?',
                             (json.dumps(meta,ensure_ascii=False),now,row['trade_date'],row['stock_code']))
        return {'inserted_rows':inserted,'filled_fields':filled,'new_state_evidence':states,'conflicts':conflicts}

    def kline_by_codes(self, day: str, codes: list[str]) -> dict[str, dict]:
        if not codes:
            return {}
        with self._conn() as conn:
            marks=','.join('?' for _ in codes)
            rows=conn.execute(f'SELECT * FROM stock_kline_daily WHERE trade_date=? AND stock_code IN ({marks})',[day,*codes]).fetchall()
        return {r['stock_code']:{**dict(r),'input_meta':json.loads(r['input_meta'] or '{}')} for r in rows}

    def kline_window(self, start: str, end: str) -> dict[str, dict[str, dict]]:
        """code -> {trade_date: row}，窗口内全部个股日K行。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT trade_date, stock_code, open, high, low, close, prev_close, volume, "
                "amount, tr_factor, action_day, close_hfq, source, input_meta FROM stock_kline_daily "
                "WHERE trade_date>=? AND trade_date<=? ORDER BY trade_date", (start, end)).fetchall()
        out: dict[str, dict[str, dict]] = {}
        for r in rows:
            d = dict(r)
            d['input_meta'] = json.loads(d.get('input_meta') or '{}')
            out.setdefault(d["stock_code"], {})[d["trade_date"]] = d
        return out

    def kline_latest_before(self, day: str) -> dict[str, dict]:
        """每只股票 day 之前最近一根K线（EOD除权检测的昨收基准）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT k.stock_code, k.trade_date, k.close FROM stock_kline_daily k "
                "JOIN (SELECT stock_code, MAX(trade_date) md FROM stock_kline_daily "
                "WHERE trade_date<? GROUP BY stock_code) m "
                "ON m.stock_code=k.stock_code AND m.md=k.trade_date", (day,)).fetchall()
        return {r["stock_code"]: {"trade_date": r["trade_date"], "close": r["close"]} for r in rows}

    def kline_coverage(self) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) n, COUNT(DISTINCT stock_code) codes, MIN(trade_date) mn, "
                "MAX(trade_date) mx FROM stock_kline_daily").fetchone()
        return {"rows": row["n"], "codes": row["codes"], "min_date": row["mn"], "max_date": row["mx"]}

    def ml_target_catalog(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT taxonomy_id, target_code, target_name, parent_taxonomy_id, parent_code, observed_at, source, '
                'verification_status, notes FROM ml_target_catalog '
                'ORDER BY taxonomy_id, target_code'
            ).fetchall()
        return [dict(row) for row in rows]

    def ml_vendor_mappings(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT taxonomy_id, target_code, vendor, vendor_code, '
                'verification_status, observed_at, notes FROM ml_vendor_mapping '
                'ORDER BY taxonomy_id, target_code, vendor'
            ).fetchall()
        return [dict(row) for row in rows]

    def ml_membership_validity(self, taxonomy_id: int | None = None,
                               target_code: str | None = None) -> list[dict]:
        query = 'SELECT * FROM ml_membership_validity'
        params: list[Any] = []
        clauses = []
        if taxonomy_id is not None:
            clauses.append('taxonomy_id=?')
            params.append(int(taxonomy_id))
        if target_code is not None:
            clauses.append('target_code=?')
            params.append(str(target_code))
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        query += ' ORDER BY taxonomy_id, target_code, effective_from, stock_code'
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def ml_r1_snapshot_save(self, taxonomy_version: str, trade_date: str, payload: dict,
                            formula_id: str = 'ml_r1', plate_type: int = 15) -> None:
        """只发布已计算的 ML-R1 输入/结果快照，保留版本和缺口报告。"""
        if payload.get('date') not in (None, trade_date):
            raise ValueError('ML-R1 snapshot date mismatch')
        evidence = payload.get('source_evidence') or (payload.get('input_snapshot') or {}).get('source_evidence')
        if not payload.get('source') or not evidence:
            raise ValueError('ML-R1 snapshot requires source evidence')
        import hashlib
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT payload FROM ml_r1_snapshots WHERE formula_id=? AND plate_type=? AND taxonomy_version=? AND trade_date=?',
                               (formula_id, int(plate_type), str(taxonomy_version), trade_date)).fetchone()
            previous = json.loads(old['payload']) if old else None
            published = payload
            if previous and previous.get('formula_version') == payload.get('formula_version') and previous.get('status') == 'final' and payload.get('status') != 'final':
                published = {**previous, 'latest_attempt': {'status': payload.get('status'),
                             'collected_at': payload.get('collected_at'), 'reasons': payload.get('reasons', [])}}
            conn.execute('INSERT OR IGNORE INTO ml_r1_revisions VALUES(?,?,?,?,?,?,?)',
                         (digest, plate_type, taxonomy_version, trade_date, payload.get('formula_version'), encoded,
                          datetime.now().isoformat(timespec='seconds')))
            conn.execute(
                'INSERT OR REPLACE INTO ml_r1_snapshots VALUES(?,?,?,?,?,?)',
                (formula_id, int(plate_type), str(taxonomy_version), trade_date,
                 json.dumps(published, ensure_ascii=False), datetime.now().isoformat(timespec='seconds')))

    def ml_r1_snapshot_get(self, taxonomy_version: str, trade_date: str,
                           formula_id: str = 'ml_r1', plate_type: int = 15) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                'SELECT payload FROM ml_r1_snapshots WHERE formula_id=? AND plate_type=? AND taxonomy_version=? AND trade_date=?',
                (formula_id, int(plate_type), str(taxonomy_version), trade_date)).fetchone()
        return json.loads(row['payload']) if row else None

    def ml_r1_snapshot_dates(self, taxonomy_version: str | None = None, limit: int = 370, plate_type: int = 15) -> list[str]:
        query = 'SELECT DISTINCT trade_date FROM ml_r1_snapshots'
        params: list[Any] = []
        query += ' WHERE plate_type=?'; params.append(int(plate_type))
        if taxonomy_version is not None:
            query += ' AND taxonomy_version=?'; params.append(str(taxonomy_version))
        query += ' ORDER BY trade_date DESC LIMIT ?'; params.append(limit)
        with self._conn() as conn:
            return [r['trade_date'] for r in conn.execute(query, params).fetchall()]

    def limit_pool_save(self, pool: str, trade_date: str, payload: dict) -> None:
        """Publish one complete dated pool. Incomplete payloads never replace a known day."""
        if payload.get('complete') is not True or payload.get('date') != trade_date:
            raise ValueError('Incomplete limit pool cannot be published')
        total = int(payload.get('total'))
        if total < 0 or len(payload.get('pool') or []) != total:
            raise ValueError('Limit pool count mismatch')
        with self._conn() as conn:
            conn.execute('INSERT OR REPLACE INTO limit_pool_snapshots VALUES(?,?,?)',
                         (pool, trade_date, json.dumps(payload, ensure_ascii=False)))

    def limit_pool_get(self, pool: str, trade_date: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute('SELECT payload FROM limit_pool_snapshots WHERE pool=? AND trade_date=?',
                               (pool, trade_date)).fetchone()
        return json.loads(row['payload']) if row else None

    def limit_pool_latest(self, pool: str, before: str | None = None) -> dict | None:
        with self._conn() as conn:
            if before:
                row = conn.execute('SELECT payload FROM limit_pool_snapshots WHERE pool=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 1',
                                   (pool, before)).fetchone()
            else:
                row = conn.execute('SELECT payload FROM limit_pool_snapshots WHERE pool=? ORDER BY trade_date DESC LIMIT 1',
                                   (pool,)).fetchone()
        return json.loads(row['payload']) if row else None


    # ---- 日内分钟采样 ----
    def index_snapshot_save(self, trade_date: str, slot: str, payload: dict) -> None:
        if payload.get('complete') is not True or payload.get('date') != trade_date:
            raise ValueError('Incomplete index snapshot cannot be published')
        if len(payload.get('items') or []) != int(payload.get('total') or -1):
            raise ValueError('Index snapshot count mismatch')
        with self._conn() as conn:
            conn.execute('INSERT OR REPLACE INTO index_snapshots VALUES(?,?,?)',
                         (trade_date, slot, json.dumps(payload, ensure_ascii=False)))

    def index_snapshot_get(self, trade_date: str, slot: str = 'latest') -> dict | None:
        with self._conn() as conn:
            if slot == 'latest':
                row = conn.execute('SELECT payload FROM index_snapshots WHERE trade_date=? ORDER BY slot DESC LIMIT 1', (trade_date,)).fetchone()
            else:
                row = conn.execute('SELECT payload FROM index_snapshots WHERE trade_date=? AND slot=?', (trade_date, slot)).fetchone()
        return json.loads(row['payload']) if row else None

    def index_snapshot_latest(self, before: str | None = None) -> dict | None:
        with self._conn() as conn:
            if before:
                row = conn.execute('SELECT payload FROM index_snapshots WHERE trade_date<=? ORDER BY trade_date DESC, slot DESC LIMIT 1', (before,)).fetchone()
            else:
                row = conn.execute('SELECT payload FROM index_snapshots ORDER BY trade_date DESC, slot DESC LIMIT 1').fetchone()
        return json.loads(row['payload']) if row else None


    def lhb_save(self, trade_date: str, payload: dict) -> None:
        if payload.get('complete') is not True or payload.get('date') != trade_date:
            raise ValueError('Incomplete LHB snapshot cannot be published')
        total = payload.get('total')
        if total is None or len(payload.get('items') or []) != int(total):
            raise ValueError('LHB snapshot count mismatch')
        with self._conn() as conn:
            conn.execute('INSERT OR REPLACE INTO lhb_snapshots VALUES(?,?)',
                         (trade_date, json.dumps(payload, ensure_ascii=False)))

    def lhb_get(self, trade_date: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute('SELECT payload FROM lhb_snapshots WHERE trade_date=?', (trade_date,)).fetchone()
        return json.loads(row['payload']) if row else None

    def lhb_seats_save(self, trade_date: str, rows: list[dict]) -> None:
        """席位明细按交易日整体替换写入。rows 字段见 _seat_row。"""
        with self._conn() as conn:
            conn.execute('DELETE FROM lhb_seats WHERE trade_date=?', (trade_date,))
            conn.executemany(
                'INSERT OR REPLACE INTO lhb_seats '
                '(trade_date, stock_code, side, dept_code, dept_name, reason, buy_amount, sell_amount, net_amount, rank, win_rate_3d) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                [(r['trade_date'], r['stock_code'], r['side'], r['dept_code'], r['dept_name'],
                  r['reason'], r['buy_amount'], r['sell_amount'], r['net_amount'], r['rank'], r['win_rate_3d'])
                 for r in rows],
            )

    def lhb_seats_get(self, trade_date: str, stock_code: str | None = None) -> list[dict]:
        sql = 'SELECT * FROM lhb_seats WHERE trade_date=?'
        args: list = [trade_date]
        if stock_code:
            sql += ' AND stock_code=?'
            args.append(stock_code)
        sql += ' ORDER BY stock_code, side, rank'
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def lhb_seats_by_dept(self, dept_code: str, page: int = 1, per_page: int = 20, stock_code: str | None = None) -> tuple[list[dict], int]:
        where = 'WHERE dept_code=?'
        args: list = [dept_code]
        if stock_code:
            where += ' AND stock_code=?'
            args.append(stock_code)
        with self._conn() as conn:
            total = conn.execute(f'SELECT COUNT(*) FROM lhb_seats {where}', args).fetchone()[0]
            rows = conn.execute(
                f'SELECT * FROM lhb_seats {where} ORDER BY trade_date DESC, stock_code LIMIT ? OFFSET ?',
                args + [per_page, (page - 1) * per_page],
            ).fetchall()
        return [dict(r) for r in rows], total

    def index_history_save(self, trade_date: str, payload: dict) -> None:
        if payload.get('complete') is not True or payload.get('date') != trade_date:
            raise ValueError('Incomplete index history cannot be published')
        if not payload.get('series'):
            raise ValueError('Index history series missing')
        with self._conn() as conn:
            conn.execute('INSERT OR REPLACE INTO index_history_snapshots VALUES(?,?)',
                         (trade_date, json.dumps(payload, ensure_ascii=False)))

    def index_history_get(self, trade_date: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute('SELECT payload FROM index_history_snapshots WHERE trade_date=?', (trade_date,)).fetchone()
        return json.loads(row['payload']) if row else None

    def index_history_latest(self) -> dict | None:
        with self._conn() as conn:
            row = conn.execute('SELECT payload FROM index_history_snapshots ORDER BY trade_date DESC LIMIT 1').fetchone()
        return json.loads(row['payload']) if row else None

    def movement_save(self, trade_date: str, payload: dict) -> None:
        if payload.get('complete') is not True or payload.get('date') != trade_date:
            raise ValueError('Incomplete movement snapshot cannot be published')
        items = payload.get('items') or []
        total = int(payload.get('total') if payload.get('total') is not None else -1)
        if total < 0 or len(items) != total:
            raise ValueError('Movement snapshot count mismatch')
        with self._conn() as conn:
            conn.execute('INSERT OR REPLACE INTO movement_snapshots VALUES(?,?)',
                         (trade_date, json.dumps(payload, ensure_ascii=False)))

    def movement_get(self, trade_date: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute('SELECT payload FROM movement_snapshots WHERE trade_date=?', (trade_date,)).fetchone()
        return json.loads(row['payload']) if row else None

    def movement_latest(self) -> dict | None:
        with self._conn() as conn:
            row = conn.execute('SELECT payload FROM movement_snapshots ORDER BY trade_date DESC LIMIT 1').fetchone()
        return json.loads(row['payload']) if row else None

    def popular_save(self, trade_date: str, payload: dict) -> None:
        if payload.get('complete') is not True or payload.get('date') != trade_date:
            raise ValueError('Incomplete popular snapshot cannot be published')
        items = payload.get('items') or []
        total = int(payload.get('total') if payload.get('total') is not None else -1)
        if total <= 0 or len(items) != total:
            raise ValueError('Popular snapshot count mismatch')
        codes = [i.get('symbol_code') for i in items]
        ranks = [i.get('rank') for i in items]
        if len(set(codes)) != len(codes):
            raise ValueError('Popular snapshot ranks or codes invalid')
        # 源站深表允许并列/非严格 1..N；公开 Top100 仍要求连续名次
        deep_sources = {'zizizaizai_ths_top_calibration', 'origin_ths_top'}
        if payload.get('source') in deep_sources or total >= 1000:
            if any(not isinstance(r, int) or r < 1 for r in ranks):
                raise ValueError('Popular snapshot ranks or codes invalid')
        elif ranks != list(range(1, total + 1)):
            raise ValueError('Popular snapshot ranks or codes invalid')
        with self._conn() as conn:
            conn.execute('INSERT OR REPLACE INTO popular_snapshots VALUES(?,?)',
                         (trade_date, json.dumps(payload, ensure_ascii=False)))

    def popular_get(self, trade_date: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute('SELECT payload FROM popular_snapshots WHERE trade_date=?', (trade_date,)).fetchone()
        return json.loads(row['payload']) if row else None

    def popular_minute_save(self, trade_date: str, minute: str, payload: dict) -> None:
        """Save an intraday minute-precision popular snapshot alongside the daily archive."""
        with self._conn() as conn:
            conn.execute('INSERT OR REPLACE INTO popular_snapshots_minutes VALUES(?,?,?)',
                         (trade_date, minute, json.dumps(payload, ensure_ascii=False)))

    def popular_minute_get(self, trade_date: str, minute: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute('SELECT payload FROM popular_snapshots_minutes WHERE trade_date=? AND minute=?',
                               (trade_date, minute)).fetchone()
        return json.loads(row['payload']) if row else None

    def popular_minute_list(self, trade_date: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute('SELECT minute, payload FROM popular_snapshots_minutes WHERE trade_date=? ORDER BY minute',
                                (trade_date,)).fetchall()
        return [json.loads(r['payload']) for r in rows]

    def popular_latest(self, before: str | None = None) -> dict | None:
        with self._conn() as conn:
            if before:
                row = conn.execute('SELECT payload FROM popular_snapshots WHERE trade_date<=? ORDER BY trade_date DESC LIMIT 1', (before,)).fetchone()
            else:
                row = conn.execute('SELECT payload FROM popular_snapshots ORDER BY trade_date DESC LIMIT 1').fetchone()
        return json.loads(row['payload']) if row else None

    def em_popular_rank_save_rows(self, rows: list[dict]) -> int:
        """rows: [{symbol_code|stock_code, trade_date, rank}, ...]"""
        if not rows:
            return 0
        payload = []
        for r in rows:
            code = r.get('symbol_code') or r.get('stock_code')
            day = r.get('trade_date')
            try:
                rk = int(r.get('rank'))
            except (TypeError, ValueError):
                continue
            if not code or not day or rk <= 0:
                continue
            payload.append((str(code).zfill(6), str(day)[:10], rk))
        if not payload:
            return 0
        with self._conn() as conn:
            conn.executemany(
                'INSERT OR REPLACE INTO em_popular_rank_hist(stock_code, trade_date, rank) VALUES(?,?,?)',
                payload,
            )
        return len(payload)

    def em_popular_rank_set_cover(self, stock_code: str, from_date: str, to_date: str, fetched_at: str) -> None:
        with self._conn() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO em_popular_rank_cover(stock_code, from_date, to_date, fetched_at) VALUES(?,?,?,?)',
                (str(stock_code).zfill(6), from_date, to_date, fetched_at),
            )

    def em_popular_rank_uncovered(self, codes: list[str], trade_date: str) -> list[str]:
        """返回 cover 未覆盖 trade_date 的代码（需拉 getHisList）。"""
        need = []
        with self._conn() as conn:
            for code in codes:
                c = str(code).zfill(6)
                row = conn.execute(
                    'SELECT from_date, to_date FROM em_popular_rank_cover WHERE stock_code=?',
                    (c,),
                ).fetchone()
                if not row or trade_date < row['from_date'] or trade_date > row['to_date']:
                    need.append(c)
        return need

    def em_popular_rank_get_many(self, trade_date: str, codes: list[str]) -> dict[str, int]:
        if not codes:
            return {}
        out: dict[str, int] = {}
        with self._conn() as conn:
            for i in range(0, len(codes), 400):
                chunk = [str(c).zfill(6) for c in codes[i:i + 400]]
                q = ','.join('?' * len(chunk))
                rows = conn.execute(
                    f'SELECT stock_code, rank FROM em_popular_rank_hist '
                    f'WHERE trade_date=? AND stock_code IN ({q})',
                    (trade_date, *chunk),
                ).fetchall()
                for r in rows:
                    out[r['stock_code']] = int(r['rank'])
        return out

    def em_popular_rank_prev(self, stock_code: str, before: str) -> int | None:
        with self._conn() as conn:
            row = conn.execute(
                'SELECT rank FROM em_popular_rank_hist WHERE stock_code=? AND trade_date<? '
                'ORDER BY trade_date DESC LIMIT 1',
                (str(stock_code).zfill(6), before),
            ).fetchone()
        return int(row['rank']) if row else None


    def block_top_save(self, trade_date: str, payload: dict) -> None:
        if payload.get('complete') is not True or payload.get('date') != trade_date:
            raise ValueError('Incomplete block_top snapshot cannot be published')
        items = payload.get('items') or []
        total = int(payload.get('total') if payload.get('total') is not None else -1)
        if total < 0 or len(items) != total:
            raise ValueError('block_top snapshot count mismatch')
        with self._conn() as conn:
            conn.execute('INSERT OR REPLACE INTO block_top_snapshots VALUES(?,?)',
                         (trade_date, json.dumps(payload, ensure_ascii=False)))

    def block_top_get(self, trade_date: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute('SELECT payload FROM block_top_snapshots WHERE trade_date=?', (trade_date,)).fetchone()
        return json.loads(row['payload']) if row else None

    def king_pool_save(self, trade_date: str, rows: list[dict]) -> None:
        """龙头池（king pool）整日快照，rows 为原站 rank 行列表。"""
        with self._conn() as conn:
            conn.execute('INSERT OR REPLACE INTO king_pool_snapshots VALUES(?,?)',
                         (trade_date, json.dumps(rows, ensure_ascii=False)))

    def king_pool_get(self, trade_date: str) -> list[dict] | None:
        with self._conn() as conn:
            row = conn.execute('SELECT payload FROM king_pool_snapshots WHERE trade_date=?', (trade_date,)).fetchone()
        return json.loads(row['payload']) if row else None

    def king_pool_latest(self, before: str | None = None) -> tuple[str, list[dict]] | None:
        """<= before 的最新龙头池快照（缺省全局最新）。"""
        with self._conn() as conn:
            if before:
                row = conn.execute(
                    'SELECT trade_date, payload FROM king_pool_snapshots WHERE trade_date<=? '
                    'ORDER BY trade_date DESC LIMIT 1', (before,)).fetchone()
            else:
                row = conn.execute(
                    'SELECT trade_date, payload FROM king_pool_snapshots ORDER BY trade_date DESC LIMIT 1').fetchone()
        return (row['trade_date'], json.loads(row['payload'])) if row else None

    def review_uplimit_save(self, trade_date: str, payload: dict) -> None:
        """复盘涨停梯队（review/uplimit/hot 不带 board）整日矩阵快照。

        payload 为原站响应 data 对象：plate/plate_info/plate_stocks/
        plate_stocks_zb/stocks(csv)/stocks_hot/ban_info/max_count/relay 等。
        board= 查询按已解码过滤语义从快照推导（见 open_routes
        ._derive_board_from_snapshot）。
        """
        with self._conn() as conn:
            conn.execute('INSERT OR REPLACE INTO review_uplimit_snapshots VALUES(?,?)',
                         (trade_date, json.dumps(payload, ensure_ascii=False)))

    def review_uplimit_get(self, trade_date: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                'SELECT payload FROM review_uplimit_snapshots WHERE trade_date=?', (trade_date,)).fetchone()
        return json.loads(row['payload']) if row else None

    def review_uplimit_latest(self, before: str | None = None) -> tuple[str, dict] | None:
        """<= before 的最新梯队矩阵快照（缺省全局最新）。"""
        with self._conn() as conn:
            if before:
                row = conn.execute(
                    'SELECT trade_date, payload FROM review_uplimit_snapshots WHERE trade_date<=? '
                    'ORDER BY trade_date DESC LIMIT 1', (before,)).fetchone()
            else:
                row = conn.execute(
                    'SELECT trade_date, payload FROM review_uplimit_snapshots ORDER BY trade_date DESC LIMIT 1').fetchone()
        return (row['trade_date'], json.loads(row['payload'])) if row else None

    def plate_popular_save(self, plate_type: int, trade_date: str, rows: list[dict]) -> None:
        """板块人气矩阵（rank/popular）整日快照。pt25 按 25 存，查询侧回落 15。"""
        with self._conn() as conn:
            conn.execute('INSERT OR REPLACE INTO plate_popular_snapshots VALUES(?,?,?)',
                         (plate_type, trade_date, json.dumps(rows, ensure_ascii=False)))

    def plate_popular_get(self, plate_type: int, trade_date: str) -> list[dict] | None:
        with self._conn() as conn:
            row = conn.execute(
                'SELECT payload FROM plate_popular_snapshots WHERE plate_type=? AND trade_date=?',
                (plate_type, trade_date)).fetchone()
        return json.loads(row['payload']) if row else None

    def plate_members_save(self, plate_type: int, trade_date: str, plate_code: str, rows: list[dict]) -> None:
        """板块成分人气排名（stocks/rank）快照，limit=100 头部行。"""
        with self._conn() as conn:
            conn.execute('INSERT OR REPLACE INTO plate_members_snapshots VALUES(?,?,?,?)',
                         (plate_type, trade_date, plate_code, json.dumps(rows, ensure_ascii=False)))

    def plate_members_get(self, plate_type: int, trade_date: str, plate_code: str) -> list[dict] | None:
        with self._conn() as conn:
            row = conn.execute(
                'SELECT payload FROM plate_members_snapshots WHERE plate_type=? AND trade_date=? AND plate_code=?',
                (plate_type, trade_date, plate_code)).fetchone()
        return json.loads(row['payload']) if row else None

    def daily_close_by_codes(self, trade_date: str, codes: list[str]) -> dict[str, dict]:
        if not codes:
            return {}
        with self._conn() as conn:
            ph = ",".join("?" * len(codes))
            rows = conn.execute(
                f"SELECT * FROM daily_close WHERE trade_date=? AND stock_code IN ({ph})",
                [trade_date, *codes],
            ).fetchall()
        return {str(r['stock_code']): dict(r) for r in rows}

    def daily_close_latest_before(self, codes: list[str], before: str) -> dict[str, dict]:
        """每只股票 <= before 的最新一行日线（停牌兜底用）。"""
        if not codes:
            return {}
        with self._conn() as conn:
            ph = ",".join("?" * len(codes))
            rows = conn.execute(
                f"""SELECT d.* FROM daily_close d
                    JOIN (SELECT stock_code, MAX(trade_date) md FROM daily_close
                          WHERE trade_date<=? AND stock_code IN ({ph})
                          GROUP BY stock_code) m
                    ON d.stock_code=m.stock_code AND d.trade_date=m.md""",
                [before, *codes],
            ).fetchall()
        return {str(r['stock_code']): dict(r) for r in rows}

    def minute_save(self, date: str, minute: str, payload: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO minute_samples(date, minute, payload) VALUES(?,?,?)",
                (date, minute, json.dumps(payload, ensure_ascii=False)),
            )

    def minute_all(self, date: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT payload FROM minute_samples WHERE date=? ORDER BY minute", (date,)
            ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def minute_dates(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT DISTINCT date FROM minute_samples ORDER BY date DESC LIMIT 60").fetchall()
        return [r["date"] for r in rows]

    # ---- 每日题材热度快照（rank 接口的历史回溯基础）----
    def topic_snapshot_save(self, trade_date: str, items: list[dict]) -> None:
        """按交易日全量覆盖保存当日题材热度排行。"""
        now = datetime.now().isoformat()
        with self._conn() as conn:
            conn.execute("DELETE FROM topic_snapshots WHERE trade_date=?", (trade_date,))
            conn.executemany(
                "INSERT OR REPLACE INTO topic_snapshots(trade_date, unique_key, payload) VALUES(?,?,?)",
                [
                    (trade_date, it.get("unique_key", ""), json.dumps(it, ensure_ascii=False))
                    for it in items
                ],
            )
        self.kv_set("topic_snapshot_saved_at", now)

    def topic_snapshot_all(self, trade_date: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT payload FROM topic_snapshots WHERE trade_date=? ORDER BY rowid", (trade_date,)
            ).fetchall()
        from app.services.ml_r1 import VERSION
        items = [json.loads(r["payload"]) for r in rows]
        for item in items:
            if item.get('formula_version') != VERSION or not item.get('input_snapshot_id'):
                # 原始 payload 留库审计；未经修正版验证的派生值退出产品查询。
                item.update(status='legacy_unverified', complete=False,
                            reasons=['legacy_derived_values_quarantined'], stocks=[])
                for field in ('score', 'today_pct', 'up_count', 'down_count', 'stock_count',
                              'up_ratio', 'leader_count', 'limit_up_count', 'limit_down_count'):
                    item[field] = None
        return items

    def topic_snapshot_dates(self, limit: int = 60) -> list[str]:
        """有题材快照的交易日（最近优先）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT trade_date FROM topic_snapshots ORDER BY trade_date DESC LIMIT ?", (limit,)
            ).fetchall()
        return [r["trade_date"] for r in rows]

    def topic_snapshot_latest_before(self, trade_date: str) -> tuple[str, list[dict]] | None:
        """返回 <= trade_date 的最近一份快照 (date, items)，无则 None。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT trade_date FROM topic_snapshots WHERE trade_date<=? ORDER BY trade_date DESC LIMIT 1",
                (trade_date,),
            ).fetchone()
        if not row:
            return None
        return row["trade_date"], self.topic_snapshot_all(row["trade_date"])

    # ---- 全市场日线收盘（pct-tier 预计算落库，对齐原站 16:00/20:00 定时任务架构）----
    def daily_close_save(self, trade_date: str, rows: list[dict], metadata: dict | None = None) -> int:
        """Only a complete, validated batch may replace a published close snapshot."""
        codes = [r.get("stock_code") for r in rows]
        if not rows or any(not c for c in codes) or len(set(codes)) != len(codes):
            raise ValueError("Empty or duplicate daily close batch")
        metadata = dict(metadata or {})
        if metadata.get("complete") is not True:
            raise ValueError("Incomplete daily close batch cannot be published")
        if metadata.get("trade_date") not in (None, trade_date):
            raise ValueError("Daily close metadata date mismatch")
        metadata.update(trade_date=trade_date, count=len(rows), complete=True)
        with self._conn() as conn:
            conn.execute("DELETE FROM daily_close WHERE trade_date=?", (trade_date,))
            conn.executemany(
                """
                INSERT INTO daily_close
                (trade_date, stock_code, stock_name, market_type, concept, close, prev_close, high, low, open, circulation_value, turnover_ratio, vol_ratio)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        trade_date,
                        r["stock_code"],
                        r.get("stock_name", ""),
                        r.get("market_type", ""),
                        r.get("concept", ""),
                        r.get("close"),
                        r.get("prev_close"),
                        r.get("high"),
                        r.get("low"),
                        r.get("open"),
                        r.get("circulation_value"),
                        r.get("turnover_ratio"),
                        r.get("vol_ratio"),
                    )
                    for r in rows
                ],
            )
            conn.execute("INSERT OR REPLACE INTO daily_close_runs VALUES(?,?)",
                         (trade_date, json.dumps(metadata, ensure_ascii=False)))
        self.kv_set("daily_close_saved_at", datetime.now().isoformat())
        return len(rows)

    def daily_close_upsert(self, rows: list[dict], *, source: str = "partial_upsert") -> int:
        """按 (trade_date, stock_code) 增量写入。不删整日、不宣称 complete。

        题材成分子集补数用；全市场定稿仍走 daily_close_save。
        """
        cleaned = []
        for r in rows:
            code = r.get("stock_code")
            day = r.get("trade_date")
            if not code or not day:
                continue
            cleaned.append(r)
        if not cleaned:
            return 0
        with self._conn() as conn:
            conn.executemany(
                """
                INSERT INTO daily_close
                (trade_date, stock_code, stock_name, market_type, concept, close, prev_close, high, low, open, circulation_value, turnover_ratio, vol_ratio)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(trade_date, stock_code) DO UPDATE SET
                  stock_name=CASE
                    WHEN excluded.stock_name IS NULL OR excluded.stock_name='' THEN daily_close.stock_name
                    ELSE excluded.stock_name END,
                  close=excluded.close,
                  prev_close=excluded.prev_close,
                  high=excluded.high,
                  low=excluded.low,
                  open=excluded.open,
                  circulation_value=COALESCE(excluded.circulation_value, daily_close.circulation_value),
                  turnover_ratio=COALESCE(excluded.turnover_ratio, daily_close.turnover_ratio),
                  vol_ratio=COALESCE(excluded.vol_ratio, daily_close.vol_ratio)
                """,
                [
                    (
                        r["trade_date"],
                        r["stock_code"],
                        r.get("stock_name", ""),
                        r.get("market_type", ""),
                        r.get("concept", ""),
                        r.get("close"),
                        r.get("prev_close"),
                        r.get("high"),
                        r.get("low"),
                        r.get("open"),
                        r.get("circulation_value"),
                        r.get("turnover_ratio"),
                        r.get("vol_ratio"),
                    )
                    for r in cleaned
                ],
            )
        self.kv_set("daily_close_partial_source", source)
        return len(cleaned)

    def daily_close_run(self, trade_date: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT metadata FROM daily_close_runs WHERE trade_date=?", (trade_date,)).fetchone()
        return json.loads(row["metadata"]) if row else None

    def daily_close_dates(self, limit: int = 60) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT DISTINCT trade_date FROM daily_close").fetchall()
        # 旧YYYYMMDD键会按字符串排在所有YYYY-MM-DD之前；先规范日期再排序去重。
        days = set()
        for row in rows:
            try:
                days.add(datetime.strptime(row['trade_date'].replace('-', ''), '%Y%m%d').date().isoformat())
            except (TypeError, ValueError):
                continue
        return sorted(days, reverse=True)[:max(0, limit)]

    def daily_close_range(self, date_end: str, days: int) -> list[dict]:
        """取 <= date_end 的最近 days 个交易日收盘数据（含 prev_close 用于区间涨幅）。"""
        with self._conn() as conn:
            dates = [
                r["trade_date"]
                for r in conn.execute(
                    "SELECT DISTINCT trade_date FROM daily_close WHERE trade_date<=? ORDER BY trade_date DESC LIMIT ?",
                    (date_end, days + 1),
                ).fetchall()
            ]
            if not dates:
                return []
            ph = ",".join("?" * len(dates))
            rows = conn.execute(
                f"SELECT * FROM daily_close WHERE trade_date IN ({ph})", dates
            ).fetchall()
        return [dict(r) for r in rows]

    def daily_close_patch_ohlc(self, trade_date: str, rows: list[dict]) -> int:
        """Fill missing open/high/low without replacing a published close snapshot."""
        if not rows:
            return 0
        updated = 0
        with self._conn() as conn:
            for row in rows:
                code = row.get("stock_code")
                if not code:
                    continue
                current = conn.execute(
                    "SELECT open, high, low, close FROM daily_close WHERE trade_date=? AND stock_code=?",
                    (trade_date, code),
                ).fetchone()
                if not current:
                    continue
                values = []
                fields = []
                for col in ("open", "high", "low"):
                    incoming = row.get(col)
                    if incoming in (None, "") or current[col] not in (None, ""):
                        continue
                    fields.append(f"{col}=?")
                    values.append(float(incoming))
                if not fields:
                    continue
                values.extend([trade_date, code])
                conn.execute(
                    f"UPDATE daily_close SET {', '.join(fields)} WHERE trade_date=? AND stock_code=?",
                    values,
                )
                updated += 1
        return updated

    def daily_close_patch_turnover(self, trade_date: str, rows: list[dict]) -> int:
        """只填缺失的 turnover_ratio / circulation_value / vol_ratio，不覆盖已发布值。

        rows 每项含 stock_code / turnover_ratio / circulation_value / vol_ratio（可只给其一）。
        """
        if not rows:
            return 0
        updated = 0
        with self._conn() as conn:
            for row in rows:
                code = row.get("stock_code")
                if not code:
                    continue
                current = conn.execute(
                    "SELECT turnover_ratio, circulation_value, vol_ratio FROM daily_close WHERE trade_date=? AND stock_code=?",
                    (trade_date, code),
                ).fetchone()
                if not current:
                    continue
                fields = []
                values = []
                for col in ("turnover_ratio", "circulation_value", "vol_ratio"):
                    incoming = row.get(col)
                    if incoming in (None, "") or current[col] not in (None, ""):
                        continue
                    fields.append(f"{col}=?")
                    values.append(float(incoming))
                if not fields:
                    continue
                values.extend([trade_date, code])
                conn.execute(
                    f"UPDATE daily_close SET {', '.join(fields)} WHERE trade_date=? AND stock_code=?",
                    values,
                )
                updated += 1
        return updated

    def daily_close_patch_concepts(self, meta: dict[str, str]) -> int:
        """用快照概念数据批量补全历史行的 concept 列（一次性维护操作）。"""
        if not meta:
            return 0
        rows = [(v, k) for k, v in meta.items() if v]
        with self._conn() as conn:
            cur = conn.executemany(
                "UPDATE daily_close SET concept = ? WHERE stock_code = ? AND (concept IS NULL OR concept = '')",
                rows,
            )
            return cur.rowcount

    # ---- 题材表格 ----
    def list_topics(self, include_deleted: bool = False) -> list[dict]:
        sql = "SELECT * FROM topics"
        if not include_deleted:
            sql += " WHERE is_deleted=0"
        sql += " ORDER BY is_top DESC, score DESC"
        with self._conn() as conn:
            rows = conn.execute(sql).fetchall()
        return [self._topic_row(r) for r in rows]

    def upsert_topic(self, topic: dict) -> dict:
        key = topic.get("unique_key") or uuid.uuid4().hex
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO topics
                (id, unique_key, name, content, rows_json, score, today_pct, up_count,
                 stock_count, up_ratio, leader_count, limit_up_count, limit_down_count, down_count,
                 reasons_json, selection_scope, is_top, is_deleted, created_time, updated_time)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    topic.get("id"),
                    key,
                    topic.get("name", ""),
                    topic.get("content", ""),
                    json.dumps(topic.get("rows", []), ensure_ascii=False),
                    topic.get("score", 0),
                    topic.get("today_pct", 0),
                    topic.get("up_count", 0),
                    topic.get("stock_count", 0),
                    topic.get("up_ratio", 0),
                    topic.get("leader_count", 0),
                    topic.get("limit_up_count", 0),
                    topic.get("limit_down_count", 0),
                    topic.get("down_count"),
                    json.dumps(topic.get("reasons", []), ensure_ascii=False),
                    topic.get("selection_scope", "all"),
                    topic.get("is_top", 0),
                    topic.get("is_deleted", 0),
                    topic.get("created_time", ""),
                    topic.get("updated_time", ""),
                ),
            )
        topic["unique_key"] = key
        return self.get_topic(key)

    def get_topic(self, unique_key: str, include_deleted: bool = True) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM topics WHERE unique_key=?", (unique_key,)).fetchone()
        if not row:
            return None
        item = self._topic_row(row)
        if item.get("is_deleted") and not include_deleted:
            return None
        return item

    def mark_topic(self, unique_key: str, **fields) -> dict | None:
        item = self.get_topic(unique_key, include_deleted=True)
        if not item:
            return None
        item.update({k: v for k, v in fields.items() if v is not None})
        return self.upsert_topic(item)

    def follow_topic(self, unique_key: str, user_id: str = "local") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO topic_follows(user_id, unique_key, created_time) VALUES(?,?,?)",
                (user_id, unique_key, datetime.now().isoformat()),
            )

    def unfollow_topic(self, unique_key: str, user_id: str = "local") -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM topic_follows WHERE user_id=? AND unique_key=?", (user_id, unique_key))

    def followed_keys(self, user_id: str = "local") -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT unique_key FROM topic_follows WHERE user_id=? ORDER BY created_time DESC",
                (user_id,),
            ).fetchall()
        return [r["unique_key"] for r in rows]

    def _topic_row(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "unique_key": row["unique_key"],
            "name": row["name"],
            "content": row["content"],
            "rows": json.loads(row["rows_json"] or "[]"),
            "score": row["score"],
            "today_pct": row["today_pct"],
            "up_count": row["up_count"],
            "stock_count": row["stock_count"],
            "up_ratio": row["up_ratio"],
            "leader_count": row["leader_count"],
            "limit_up_count": row["limit_up_count"],
            "limit_down_count": row["limit_down_count"],
            "down_count": row["down_count"],
            "reasons": json.loads(row["reasons_json"] or "[]"),
            "selection_scope": row["selection_scope"],
            "is_top": bool(row["is_top"]),
            "is_deleted": bool(row["is_deleted"]),
            "created_time": row["created_time"],
            "updated_time": row["updated_time"],
        }

    def topic_public(self, item: dict, *, followed: bool = False) -> dict:
        rows = item.get("rows") or []
        names = [str(r.get("个股") or r.get("stock_name") or "").strip() for r in rows]
        names = [n for n in names if n]
        return {
            **item,
            "id": item.get("unique_key"),
            "unique_key": item.get("unique_key"),
            "topic_id": item.get("unique_key"),
            "stock_count": item.get("stock_count") or len(names),
            "down_count": item.get("down_count"),
            "is_followed": bool(followed),
            "is_top": 1 if item.get("is_top") else 0,
            "is_deleted": 1 if item.get("is_deleted") else 0,
        }

    def ai_report_save(self, report_type: str, trade_date: str, payload: dict) -> dict:
        item = dict(payload)
        report_id = item.get("id") or f"{report_type}:{trade_date}"
        item.update(id=report_id, type=report_type, trade_date=trade_date)
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ai_reports(id, report_type, trade_date, payload) VALUES(?,?,?,?)",
                (report_id, report_type, trade_date, json.dumps(item, ensure_ascii=False)),
            )
        return item

    def ai_report_get(self, report_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT payload FROM ai_reports WHERE id=?", (report_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def ai_report_list(self, report_type: str | None = None) -> list[dict]:
        with self._conn() as conn:
            if report_type:
                rows = conn.execute(
                    "SELECT payload FROM ai_reports WHERE report_type=? ORDER BY trade_date DESC, id DESC",
                    (report_type,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT payload FROM ai_reports ORDER BY trade_date DESC, id DESC").fetchall()
        return [json.loads(r["payload"]) for r in rows]


store = Store()
