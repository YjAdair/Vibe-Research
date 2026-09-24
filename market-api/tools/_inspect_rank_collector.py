"""Inspect local plate_rank today + collector heartbeat."""
import json
import sqlite3
from pathlib import Path

db = Path(__file__).resolve().parents[1] / "zzquant.db"
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

print("=== plate_rank_daily 2026-09-24 top12 (from payload) ===")
rows = con.execute(
    """
    SELECT plate_code, payload, money_leader_exact
    FROM plate_rank_daily
    WHERE plate_type=17 AND trade_date='2026-09-24'
    """
).fetchall()
parsed = []
for r in rows:
    p = json.loads(r["payload"]) if isinstance(r["payload"], str) else (r["payload"] or {})
    parsed.append(
        {
            "plate_code": r["plate_code"],
            "name": p.get("plate_name"),
            "score": p.get("score"),
            "rate": p.get("rate"),
            "source": p.get("source"),
            "source_as_of": p.get("source_as_of"),
            "collected_at": p.get("collected_at"),
        }
    )
parsed.sort(key=lambda x: -(x["score"] if isinstance(x["score"], (int, float)) else -1e18))
for i, r in enumerate(parsed[:12], 1):
    print(i, r)

print("\n=== kv / collector ===")
tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("kv-ish", [t for t in tables if "kv" in t.lower() or "collect" in t.lower()])
for t in tables:
    if "kv" in t.lower():
        cols = [r[1] for r in con.execute(f"pragma table_info({t})")]
        print(t, cols)
        try:
            sample = con.execute(f"SELECT * FROM {t} WHERE key LIKE '%collector%' OR k LIKE '%collector%' LIMIT 5").fetchall()
        except Exception:
            try:
                sample = con.execute(f"SELECT * FROM {t} LIMIT 3").fetchall()
            except Exception as e:
                sample = [str(e)]
        for s in sample:
            print(" ", dict(s) if hasattr(s, "keys") else s)

if "collector_runs" in tables:
    print("\n=== recent kaipanla collector_runs ===")
    runs = con.execute(
        """
        SELECT job, slot, status, started_at, finished_at, substr(coalesce(detail,''),1,120) AS detail
        FROM collector_runs
        WHERE job LIKE 'kaipanla%'
        ORDER BY started_at DESC LIMIT 20
        """
    ).fetchall()
    for r in runs:
        print(dict(r))
