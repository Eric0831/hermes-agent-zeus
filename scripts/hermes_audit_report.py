#!/usr/bin/env python3
"""Federation audit report — sample key Phase 0-E metrics across all
six Hermes gateways (main + 5 apostles) and emit a flat text summary.

Usage:
    ./scripts/hermes_audit_report.py                # text to stdout
    ./scripts/hermes_audit_report.py --json         # machine-readable
    ./scripts/hermes_audit_report.py --snapshot     # save to ~/.hermes/audit/
    ./scripts/hermes_audit_report.py --diff         # diff vs latest snapshot

Meant to be run from cron / systemd timer for trend detection.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_state import SessionDB  # noqa: E402


GATEWAYS = {
    "main": Path.home() / ".hermes" / "state.db",
    "ocean": Path.home() / "zeus" / "agents" / "ocean" / "eos" / "state.db",
    "eleven": Path.home() / "zeus" / "agents" / "eleven" / "eos" / "state.db",
    "wilson": Path.home() / "zeus" / "agents" / "wilson" / "eos" / "state.db",
    "susan": Path.home() / "zeus" / "agents" / "susan" / "eos" / "state.db",
    "crypto": Path.home() / "zeus" / "agents" / "crypto" / "eos" / "state.db",
}

AUDIT_DIR = Path.home() / ".hermes" / "audit"


def _q(db: SessionDB, sql: str, params: tuple = ()) -> int:
    try:
        row = db._conn.execute(sql, params).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return 0


_GATEWAY_OWN_CLUSTER = {
    "main": "hermes-primary",
    "ocean": "OCEAN",
    "eleven": "ELEVEN",
    "wilson": "WILSON",
    "susan": "SUSAN",
    "crypto": "CRYPTO",
}


def _self_trust(db: SessionDB, gateway: str) -> float:
    name = _GATEWAY_OWN_CLUSTER.get(gateway)
    if not name:
        return 0.0
    try:
        row = db._conn.execute(
            "SELECT trust_score FROM agent_clusters WHERE cluster_name=? AND status='active'",
            (name,),
        ).fetchone()
        return round(float(row[0]), 3) if row and row[0] is not None else 0.0
    except Exception:
        return 0.0


def collect(gateway: str, path: Path) -> dict:
    if not path.exists():
        return {"gateway": gateway, "present": False}
    db = SessionDB(path)
    evid_total = _q(db, "SELECT COUNT(*) FROM evidence_records")
    evid_named = _q(
        db,
        "SELECT COUNT(*) FROM evidence_records WHERE tool_name IS NOT NULL AND tool_name NOT IN ('', 'unknown')",
    )
    finding_types = [
        r[0]
        for r in db._conn.execute(
            "SELECT DISTINCT finding_type FROM meta_learning_findings"
        ).fetchall()
    ]
    return {
        "gateway": gateway,
        "present": True,
        "sessions": _q(db, "SELECT COUNT(*) FROM sessions"),
        "tasks_total": _q(db, "SELECT COUNT(*) FROM tasks"),
        "tasks_completed": _q(db, "SELECT COUNT(*) FROM tasks WHERE status='completed'"),
        "tasks_failed": _q(db, "SELECT COUNT(*) FROM tasks WHERE status='failed'"),
        "tasks_cancelled": _q(db, "SELECT COUNT(*) FROM tasks WHERE status='cancelled'"),
        "tasks_stale": _q(
            db,
            "SELECT COUNT(*) FROM tasks WHERE status IN ('running','planned','triaged','verifying','blocked')",
        ),
        "evidence_total": evid_total,
        "evidence_named": evid_named,
        "evidence_named_pct": round(100.0 * evid_named / evid_total, 1) if evid_total else 0.0,
        "policy_evaluations": _q(db, "SELECT COUNT(*) FROM policy_evaluations"),
        "policy_auto_allow": _q(db, "SELECT COUNT(*) FROM policy_evaluations WHERE decision='allow'"),
        "policy_needs_approval": _q(
            db, "SELECT COUNT(*) FROM policy_evaluations WHERE decision='allow_with_approval'"
        ),
        "clusters_active": _q(db, "SELECT COUNT(*) FROM agent_clusters WHERE status='active'"),
        # Self-trust: this gateway's own cluster entry (hermes-primary in
        # main, OCEAN/ELEVEN/WILSON/SUSAN/CRYPTO in the respective apostles)
        "self_trust": _self_trust(db, gateway),
        "meta_runs": _q(db, "SELECT COUNT(*) FROM meta_learning_runs"),
        "meta_findings": _q(db, "SELECT COUNT(*) FROM meta_learning_findings"),
        "meta_finding_types": sorted(finding_types),
        "proposals_total": _q(db, "SELECT COUNT(*) FROM capability_proposals"),
        "proposals_proposed": _q(db, "SELECT COUNT(*) FROM capability_proposals WHERE status='proposed'"),
        "proposals_approved": _q(db, "SELECT COUNT(*) FROM capability_proposals WHERE status='approved'"),
        "proposals_incubating": _q(db, "SELECT COUNT(*) FROM capability_proposals WHERE status='incubating'"),
        "capability_versions_total": _q(db, "SELECT COUNT(*) FROM capability_versions"),
        # Phase E flow = versions that came from a real proposal (exclude
        # the brain-evolution bootstrap seed rows which live forever in
        # status='proposed' with source_proposal_id=NULL and are not
        # meant to move through the lifecycle).
        "capability_versions_flow": _q(
            db,
            "SELECT COUNT(*) FROM capability_versions WHERE source_proposal_id IS NOT NULL",
        ),
        "capability_versions_bootstrap": _q(
            db,
            "SELECT COUNT(*) FROM capability_versions WHERE source_proposal_id IS NULL",
        ),
        "capability_versions_incubating": _q(
            db, "SELECT COUNT(*) FROM capability_versions WHERE status='incubating'"
        ),
        "capability_versions_adopted": _q(
            db, "SELECT COUNT(*) FROM capability_versions WHERE status='adopted'"
        ),
        "skills_active": _q(db, "SELECT COUNT(*) FROM skill_registry WHERE status='active'"),
        "skills_candidate": _q(db, "SELECT COUNT(*) FROM skill_registry WHERE status='candidate'"),
    }


def collect_all() -> dict:
    return {
        "timestamp": time.time(),
        "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "gateways": {name: collect(name, path) for name, path in GATEWAYS.items()},
    }


def format_text(snapshot: dict) -> str:
    lines = [
        f"Hermes Federation Audit  {snapshot['timestamp_iso']}",
        "=" * 80,
    ]
    header = (
        f"{'gateway':10s} {'sess':>5s} {'tasks':>6s} {'OK':>4s} {'FAIL':>5s} "
        f"{'stale':>5s} {'evid':>6s} {'named%':>7s} {'pol':>5s} {'!allow':>7s} "
        f"{'runs':>5s} {'find':>5s} {'types':>5s} {'prop':>4s} {'capv_f':>6s} "
        f"{'capv_b':>6s} {'trust':>6s}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for name, g in snapshot["gateways"].items():
        if not g.get("present"):
            lines.append(f"{name:10s} (state.db not found)")
            continue
        lines.append(
            f"{g['gateway']:10s} "
            f"{g['sessions']:>5d} "
            f"{g['tasks_total']:>6d} "
            f"{g['tasks_completed']:>4d} "
            f"{g['tasks_failed']:>5d} "
            f"{g['tasks_stale']:>5d} "
            f"{g['evidence_total']:>6d} "
            f"{g['evidence_named_pct']:>6.1f}% "
            f"{g['policy_evaluations']:>5d} "
            f"{g['policy_needs_approval']:>7d} "
            f"{g['meta_runs']:>5d} "
            f"{g['meta_findings']:>5d} "
            f"{len(g['meta_finding_types']):>5d} "
            f"{g['proposals_total']:>4d} "
            f"{g['capability_versions_flow']:>6d} "
            f"{g['capability_versions_bootstrap']:>6d} "
            f"{g['self_trust']:>6.3f}"
        )
    lines.append("")
    lines.append("Legend:")
    lines.append("  stale        — tasks stuck in running/planned/triaged/verifying/blocked")
    lines.append("  named%       — evidence with a real tool_name (target: 100%)")
    lines.append("  !allow       — policy decisions that weren't plain 'allow'")
    lines.append("  types        — distinct meta_learning finding_type values (target: >=3)")
    lines.append("  prop         — total capability_proposals")
    lines.append("  capv_f       — capability_versions with source_proposal_id (Phase E flow)")
    lines.append("  capv_b       — capability_versions from brain-evolution bootstrap (static baseline)")
    lines.append("  trust        — this gateway's own agent_clusters trust_score [0.1, 0.9]")
    return "\n".join(lines)


def diff(prev: dict, curr: dict) -> str:
    lines = [f"Diff  {prev['timestamp_iso']} -> {curr['timestamp_iso']}", "-" * 80]
    metrics = [
        "tasks_total", "tasks_completed", "tasks_failed", "tasks_stale",
        "evidence_total", "evidence_named",
        "policy_evaluations", "policy_needs_approval",
        "meta_runs", "meta_findings",
        "proposals_total", "proposals_incubating",
        "capability_versions_total", "capability_versions_incubating",
    ]
    for name, curr_g in curr["gateways"].items():
        prev_g = prev.get("gateways", {}).get(name, {})
        if not curr_g.get("present") or not prev_g.get("present"):
            continue
        deltas = {m: curr_g.get(m, 0) - prev_g.get(m, 0) for m in metrics}
        moved = {k: v for k, v in deltas.items() if v != 0}
        if moved:
            parts = ", ".join(f"{k}+={v:+d}" for k, v in moved.items())
            lines.append(f"{name:10s} {parts}")
        else:
            lines.append(f"{name:10s} (no change)")
    return "\n".join(lines)


def latest_snapshot() -> dict | None:
    if not AUDIT_DIR.exists():
        return None
    snaps = sorted(AUDIT_DIR.glob("snapshot_*.json"))
    if not snaps:
        return None
    try:
        with open(snaps[-1]) as f:
            return json.load(f)
    except Exception:
        return None


def save_snapshot(snapshot: dict) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    fname = AUDIT_DIR / f"snapshot_{int(snapshot['timestamp'])}.json"
    with open(fname, "w") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    return fname


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--snapshot", action="store_true", help="save snapshot to disk")
    parser.add_argument("--diff", action="store_true", help="diff vs latest snapshot")
    args = parser.parse_args()

    curr = collect_all()

    if args.diff:
        prev = latest_snapshot()
        if prev is None:
            print("(no prior snapshot to diff against)")
        else:
            print(diff(prev, curr))
        print()

    if args.json:
        json.dump(curr, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print(format_text(curr))

    if args.snapshot:
        path = save_snapshot(curr)
        print(f"\nsnapshot saved: {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
