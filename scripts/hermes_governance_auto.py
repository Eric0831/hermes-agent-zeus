#!/usr/bin/env python3
"""Automated governance pass — auto-approve low-risk capability_proposals.

Rules (conservative by design):
  - status must be 'proposed'
  - risk_score <= MAX_RISK (default 0.20)
  - expected_gain >= MIN_GAIN (default 0.30)
  - action_hint.kind must be in SAFE_KINDS (only types whose executor
    writes to a 'proposed' / 'candidate' sink — never to an already-
    active config)
  - per-gateway per-run cap MAX_PER_RUN (default 3) prevents a single
    noisy meta_learning cycle from flooding governance

Proposals that pass are auto-approved and auto-promoted to
capability_version (incubating). The version's action_hint is NOT
auto-executed — /tasks execute is still a manual trigger so humans
see what automation is about to do.

Usage:
    ./scripts/hermes_governance_auto.py                # dry-run
    ./scripts/hermes_governance_auto.py --apply        # write
    ./scripts/hermes_governance_auto.py --max-risk 0.3 # tune
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_state import SessionDB  # noqa: E402
from brain import evolution_architect, capability_manager, governance  # noqa: E402


GATEWAYS = {
    "main": Path.home() / ".hermes" / "state.db",
    "ocean": Path.home() / "zeus" / "agents" / "ocean" / "eos" / "state.db",
    "eleven": Path.home() / "zeus" / "agents" / "eleven" / "eos" / "state.db",
    "wilson": Path.home() / "zeus" / "agents" / "wilson" / "eos" / "state.db",
    "susan": Path.home() / "zeus" / "agents" / "susan" / "eos" / "state.db",
    "crypto": Path.home() / "zeus" / "agents" / "crypto" / "eos" / "state.db",
}

# Only action_hint kinds whose executor writes to a reviewable / gated
# sink. If an executor ever starts writing directly to active config,
# remove it from this allowlist.
SAFE_KINDS = {
    "extract_skill",         # writes skill_registry in 'candidate'
    "extract_precedents",    # writes precedent_records (non-binding)
    "update_recommended_tools",  # writes doctrine_registry in 'proposed'
}


def eligible(prop: dict, max_risk: float, min_gain: float) -> tuple[bool, str]:
    """Return (eligible, reason) for a proposal."""
    if prop.get("status") != "proposed":
        return False, f"status={prop.get('status')}"
    risk = float(prop.get("risk_score") or 1.0)
    gain = float(prop.get("expected_gain") or 0.0)
    if risk > max_risk:
        return False, f"risk_score={risk:.2f} > {max_risk}"
    if gain < min_gain:
        return False, f"expected_gain={gain:.2f} < {min_gain}"
    try:
        pj = json.loads(prop.get("proposal_json") or "{}")
    except Exception:
        pj = {}
    kind = (pj.get("action_hint") or {}).get("kind") or ""
    if kind not in SAFE_KINDS:
        return False, f"action_hint.kind='{kind}' not in SAFE_KINDS"
    return True, "ok"


def process_gateway(
    name: str, path: Path, *,
    apply: bool, max_risk: float, min_gain: float, per_run_cap: int,
) -> dict:
    if not path.exists():
        return {"gateway": name, "skipped": "no_state_db"}
    db = SessionDB(path)
    props = evolution_architect.get_proposals(db, status="proposed", limit=50)
    approved: list[dict] = []
    skipped: list[tuple[str, str]] = []

    for p in props:
        if len(approved) >= per_run_cap:
            skipped.append((p["id"], "per_run_cap_reached"))
            continue
        ok, reason = eligible(p, max_risk, min_gain)
        if not ok:
            skipped.append((p["id"], reason))
            continue

        if apply:
            kind = (
                json.loads(p.get("proposal_json") or "{}")
                .get("action_hint", {})
                .get("kind")
            )
            # Log the decision to governance_reviews BEFORE state change
            # so the audit trail survives even if the promote fails.
            governance.review_proposal(
                db,
                subject_type="capability_proposal",
                subject_id=p["id"],
                risk_score=float(p.get("risk_score") or 0.0),
                decision="approve",
                notes=(
                    f"auto_governance kind={kind} "
                    f"gain={p.get('expected_gain')} risk={p.get('risk_score')}"
                ),
                reviewer_id="auto_governance",
            )
            evolution_architect.update_proposal_status(
                db, p["id"], "approved",
                reason=(
                    f"auto_governance (risk<={max_risk}, gain>={min_gain}, "
                    f"kind={kind})"
                ),
            )
            try:
                vid = capability_manager.promote_from_proposal(db, p["id"])
                approved.append({"proposal": p["id"], "version": vid})
            except Exception as exc:
                # rollback the approve-only if we can't promote — keep
                # pipeline honest
                governance.review_proposal(
                    db,
                    subject_type="capability_proposal",
                    subject_id=p["id"],
                    risk_score=float(p.get("risk_score") or 0.0),
                    decision="reject",
                    notes=f"auto_governance_promote_failed: {exc}",
                    reviewer_id="auto_governance",
                )
                evolution_architect.update_proposal_status(
                    db, p["id"], "rejected",
                    reason=f"auto_governance_promote_failed: {exc}",
                )
                skipped.append((p["id"], f"promote_failed: {exc}"))
        else:
            approved.append({"proposal": p["id"], "version": "DRY_RUN"})

    return {
        "gateway": name,
        "approved": approved,
        "skipped": skipped,
        "total_proposed_scanned": len(props),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write approvals (default: dry-run)")
    parser.add_argument("--max-risk", type=float, default=0.20)
    parser.add_argument("--min-gain", type=float, default=0.30)
    parser.add_argument("--per-run-cap", type=int, default=3,
                        help="max approvals per gateway per invocation")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"Hermes governance auto-approve — {mode} "
          f"(max_risk={args.max_risk}, min_gain={args.min_gain}, "
          f"cap={args.per_run_cap}/gw)")
    print("=" * 72)

    total_approved = 0
    for name, path in GATEWAYS.items():
        r = process_gateway(
            name, path,
            apply=args.apply,
            max_risk=args.max_risk,
            min_gain=args.min_gain,
            per_run_cap=args.per_run_cap,
        )
        if "skipped" in r and r.get("skipped") == "no_state_db":
            print(f"{name:8s} (no state.db)")
            continue
        approved = r.get("approved", [])
        total_approved += len(approved)
        print(f"{name:8s} scanned={r['total_proposed_scanned']:>3d}  "
              f"approved={len(approved):>2d}  "
              f"skipped={len(r.get('skipped', [])):>2d}")
        for a in approved:
            print(f"           approved {a['proposal']} -> {a['version']}")

    print()
    print(f"Total approved: {total_approved}  ({'wrote changes' if args.apply else 'dry-run — use --apply to commit'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
