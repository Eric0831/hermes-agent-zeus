#!/usr/bin/env python3
"""Federation skill_registry auto-promotion.

Finds low-risk skill_registry candidates that have been used enough
times with high-enough success to be safely promoted to 'active', then
calls brain.skill_engine.auto_promote (which also refuses non-low-risk
candidates). Conservative gates so a lightly-used or tool-risky skill
can't grab active status.

Default thresholds:
  min_usage     >= 5   (at least 5 recorded applications)
  min_success   >= 0.7 (recorded success_rate)
  cap_per_run   = 5   per gateway

Logs every promotion to governance_reviews with
reviewer_id='skill_promoter' so /tasks governance shows activity.

Usage:
    ./scripts/hermes_skill_auto_promote.py           # dry-run
    ./scripts/hermes_skill_auto_promote.py --apply   # commit
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_state import SessionDB  # noqa: E402
from brain import skill_engine, governance  # noqa: E402


GATEWAYS = {
    "main": Path.home() / ".hermes" / "state.db",
    "ocean": Path.home() / "zeus" / "agents" / "ocean" / "eos" / "state.db",
    "eleven": Path.home() / "zeus" / "agents" / "eleven" / "eos" / "state.db",
    "wilson": Path.home() / "zeus" / "agents" / "wilson" / "eos" / "state.db",
    "susan": Path.home() / "zeus" / "agents" / "susan" / "eos" / "state.db",
    "crypto": Path.home() / "zeus" / "agents" / "crypto" / "eos" / "state.db",
}


def candidates_ready(db: SessionDB, min_usage: int, min_success: float, cap: int) -> list[dict]:
    rows = db._conn.execute(
        """SELECT id, skill_name, intent_family, usage_count, success_rate,
                  risk_level, source_task_id
           FROM skill_registry
           WHERE status = 'candidate'
                 AND risk_level = 'low'
                 AND usage_count >= ?
                 AND success_rate >= ?
           ORDER BY success_rate DESC, usage_count DESC
           LIMIT ?""",
        (min_usage, min_success, cap),
    ).fetchall()
    return [dict(r) for r in rows]


def process(
    name: str, path: Path, *,
    apply: bool, min_usage: int, min_success: float, cap: int,
) -> dict:
    if not path.exists():
        return {"gateway": name, "skipped": "no_state_db"}
    db = SessionDB(path)
    cands = candidates_ready(db, min_usage, min_success, cap)
    promoted: list[str] = []
    for c in cands:
        if apply:
            ok = skill_engine.auto_promote(db, c["id"])
            if ok:
                governance.review_proposal(
                    db,
                    subject_type="skill_registry",
                    subject_id=c["id"],
                    risk_score=0.0,
                    decision="promote",
                    notes=(
                        f"skill '{c['skill_name']}' usage={c['usage_count']} "
                        f"success_rate={c['success_rate']:.2f}"
                    ),
                    reviewer_id="skill_promoter",
                )
                promoted.append(c["id"])
        else:
            promoted.append(f"DRY:{c['id']}")
    # Candidate pool snapshot (for observability, not just eligible ones)
    pool = db._conn.execute(
        "SELECT COUNT(*) FROM skill_registry WHERE status = 'candidate'"
    ).fetchone()[0]
    return {
        "gateway": name,
        "candidate_pool": pool,
        "eligible_now": len(cands),
        "promoted": promoted,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--min-usage", type=int, default=5)
    parser.add_argument("--min-success", type=float, default=0.7)
    parser.add_argument("--cap", type=int, default=5)
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"Hermes skill auto-promote — {mode} "
        f"(usage>={args.min_usage}, success>={args.min_success}, cap={args.cap}/gw)"
    )
    print("=" * 72)
    grand = 0
    for name, path in GATEWAYS.items():
        r = process(
            name, path, apply=args.apply,
            min_usage=args.min_usage, min_success=args.min_success, cap=args.cap,
        )
        if r.get("skipped"):
            print(f"{name:8s} (no state.db)")
            continue
        promoted = r.get("promoted", [])
        grand += 0 if not args.apply else len(promoted)
        print(
            f"{name:8s} candidates_pool={r['candidate_pool']:>3d}  "
            f"eligible={r['eligible_now']:>2d}  "
            f"promoted={len(promoted):>2d}"
        )
        for sid in promoted[:3]:
            print(f"           {sid}")
    print()
    print(
        f"Total promoted: {grand}"
        + ("" if args.apply else "  (dry-run — use --apply)")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
