"""Smoke: ladder works for top themes on rank calendar, not chip-only."""
from __future__ import annotations

import json

from app.core.store import store
from app.services import plate_flow
from app.services.ladder_clean import build_ladder_rows


def top_plates(day: str, n: int = 8) -> list[tuple[str, str, float]]:
    rows = store.plate_rank_range(17, day, day)
    scored = []
    for r in rows:
        p = r.get("payload")
        if isinstance(p, str):
            p = json.loads(p)
        elif not isinstance(p, dict):
            p = r
        code = str(r.get("plate_code") or p.get("plate_code") or "")
        name = str(p.get("plate_name") or p.get("name") or "")
        score = float(p.get("score") or 0)
        if code:
            scored.append((code, name, score))
    scored.sort(key=lambda x: -x[2])
    return scored[:n]


def main() -> None:
    days = store.plate_rank_dates(17, 10)
    latest = days[0]
    tops = top_plates(latest, 6)
    print("latest", latest, "tops", [(c, n) for c, n, _ in tops])
    print("day | " + " | ".join(c for c, _, _ in tops))
    empty = []
    for day in days:
        cells = []
        for code, name, _ in tops:
            members = plate_flow.plate_members_full(code, day)
            built = build_ladder_rows(day, members)
            n = len(built["sealed"]) + len(built["broken"])
            src = built["pool_source"] or "-"
            cells.append(f"{n}/{src[:2]}")
            if n == 0 and src != "-":
                empty.append((day, code, name, len(members), src))
            if not members:
                empty.append((day, code, name, 0, "no_members"))
        print(day, "|", " | ".join(cells))
    if empty:
        print("gaps", empty[:20])
    else:
        print("ok: every top theme x rank day has members and non-empty ladder or explicit empty intersection")


if __name__ == "__main__":
    main()
