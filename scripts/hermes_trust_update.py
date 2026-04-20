#!/usr/bin/env python3
"""Federation trust-score update.

Reads each gateway's state.db, computes a task-success signal over the
last TRUST_WINDOW_DAYS days, and nudges the corresponding cluster's
trust_score in that gateway's agent_clusters table. Each gateway
updates its own hermes-primary cluster (one-per-gateway authority) and,
where the apostle name matches a registered cluster, that too.

Signal → delta (bounded at +/- 0.05 per run):
  completion_rate >= 0.90    → +0.05
  completion_rate >= 0.80    → +0.02
  completion_rate in 0.60-0.80 → 0
  completion_rate in 0.40-0.60 → -0.02
  completion_rate  < 0.40    → -0.05
  no tasks in window         → -0.01 (stale penalty)

Trust stays in [0.1, 0.9] — never adopts extreme values so a momentary
hiccup doesn't destroy an apostle's reputation. Log to governance_
reviews via reviewer_id='trust_updater' for audit.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_state import SessionDB  # noqa: E402
from brain import agent_society, governance  # noqa: E402


GATEWAYS = {
    "main": ("hermes-primary", Path.home() / ".hermes" / "state.db"),
    "ocean": ("OCEAN", Path.home() / "zeus" / "agents" / "ocean" / "eos" / "state.db"),
    "eleven": ("ELEVEN", Path.home() / "zeus" / "agents" / "eleven" / "eos" / "state.db"),
    "wilson": ("WILSON", Path.home() / "zeus" / "agents" / "wilson" / "eos" / "state.db"),
    "susan": ("SUSAN", Path.home() / "zeus" / "agents" / "susan" / "eos" / "state.db"),
    "crypto": ("CRYPTO", Path.home() / "zeus" / "agents" / "crypto" / "eos" / "state.db"),
}

TRUST_WINDOW_DAYS = 7
MIN_TRUST = 0.1
MAX_TRUST = 0.9
STALE_PENALTY = -0.01


def signal_to_delta(total: int, completed: int, failed: int) -> tuple[float, str]:
    if total == 0:
        return STALE_PENALTY, "no_tasks_in_window"
    rate = completed / total if total else 0.0
    if rate >= 0.90:
        return 0.05, f"excellent_rate={rate:.2f}"
    if rate >= 0.80:
        return 0.02, f"good_rate={rate:.2f}"
    if rate >= 0.60:
        return 0.0, f"acceptable_rate={rate:.2f}"
    if rate >= 0.40:
        return -0.02, f"poor_rate={rate:.2f}"
    return -0.05, f"failing_rate={rate:.2f}"


def compute_window_stats(db: SessionDB, window_seconds: float) -> tuple[int, int, int]:
    cutoff = time.time() - window_seconds
    row = db._conn.execute(
        """SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
              SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
           FROM tasks WHERE created_at >= ?""",
        (cutoff,),
    ).fetchone()
    if not row:
        return 0, 0, 0
    return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)


def apply_trust_update(
    db: SessionDB, cluster_name: str, delta: float, reason: str, apply: bool,
) -> dict:
    row = db._conn.execute(
        "SELECT id, trust_score FROM agent_clusters WHERE cluster_name = ? AND status = 'active'",
        (cluster_name,),
    ).fetchone()
    if not row:
        return {"cluster": cluster_name, "found": False}
    rd = dict(row) if hasattr(row, "keys") else {"id": row[0], "trust_score": row[1]}
    current = float(rd["trust_score"])
    # Keep trust scores at 3 decimal precision so the /tasks clusters
    # output and governance_reviews notes read cleanly instead of showing
    # floating-point artefacts like 0.6000000000000001.
    new = round(max(MIN_TRUST, min(MAX_TRUST, current + delta)), 3)
    if new == current:
        return {"cluster": cluster_name, "id": rd["id"], "trust_before": current,
                "trust_after": new, "delta": 0.0, "reason": reason, "applied": False}
    if apply:
        # Use update_trust which applies delta; but we want absolute-bounded
        # behaviour so we compute delta ourselves first.
        agent_society.update_trust(db, rd["id"], new - current)
        governance.review_proposal(
            db,
            subject_type="agent_cluster",
            subject_id=rd["id"],
            risk_score=0.0,
            decision="trust_update",
            notes=(
                f"{cluster_name}: {current:.3f} -> {new:.3f} "
                f"(delta={new - current:+.3f}, reason={reason})"
            ),
            reviewer_id="trust_updater",
        )
    return {
        "cluster": cluster_name, "id": rd["id"],
        "trust_before": current, "trust_after": new,
        "delta": new - current, "reason": reason, "applied": apply,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write updates (default: dry-run)")
    parser.add_argument("--window-days", type=int, default=TRUST_WINDOW_DAYS)
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    window = args.window_days * 86400
    print(f"Hermes trust update — {mode} (window={args.window_days}d)")
    print("=" * 72)

    for name, (cluster_name, path) in GATEWAYS.items():
        if not path.exists():
            print(f"{name:8s} (no state.db)")
            continue
        db = SessionDB(path)
        total, completed, failed = compute_window_stats(db, window)
        delta, reason = signal_to_delta(total, completed, failed)
        res = apply_trust_update(db, cluster_name, delta, reason, args.apply)
        if not res.get("found", True):
            print(f"{name:8s} window(total={total} ok={completed} fail={failed})  "
                  f"cluster '{cluster_name}' NOT FOUND")
            continue
        print(
            f"{name:8s} window(total={total} ok={completed} fail={failed})  "
            f"trust {res['trust_before']:.3f} -> {res['trust_after']:.3f} "
            f"({res['delta']:+.3f})  reason={res['reason']}"
        )

    print()
    print("(dry-run — use --apply to commit)" if not args.apply else "Applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
