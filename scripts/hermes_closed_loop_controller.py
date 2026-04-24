#!/usr/bin/env python3
"""Conservative AgentEOS closed-loop controller.

Moves safe, low-risk capability_versions through:

    incubating -> experimental -> execute action_hint -> limited_rollout

The controller deliberately stops before adoption. Adoption is where a
capability can become active behavior, so it remains operator-gated until
there is a separate canary evaluator with enough post-rollout evidence.

Default mode is dry-run. Use --apply to write.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_state import SessionDB  # noqa: E402
from brain import capability_manager, governance  # noqa: E402


GATEWAYS = {
    "main": Path.home() / ".hermes" / "state.db",
    "ocean": Path.home() / "zeus" / "agents" / "ocean" / "eos" / "state.db",
    "eleven": Path.home() / "zeus" / "agents" / "eleven" / "eos" / "state.db",
    "wilson": Path.home() / "zeus" / "agents" / "wilson" / "eos" / "state.db",
    "susan": Path.home() / "zeus" / "agents" / "susan" / "eos" / "state.db",
    "crypto": Path.home() / "zeus" / "agents" / "crypto" / "eos" / "state.db",
}

# Safe sinks only:
# - extract_skill writes skill_registry(candidate)
# - extract_precedents writes precedent_records
# - update_recommended_tools writes doctrine_registry(proposed)
DEFAULT_SAFE_KINDS = {
    "extract_skill",
    "extract_precedents",
    "update_recommended_tools",
}


def _q(db: SessionDB, sql: str, params: tuple = ()) -> int:
    try:
        row = db._conn.execute(sql, params).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return 0


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _load_json(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _ensure_run_table(db: SessionDB) -> None:
    def _do(conn):
        conn.execute(
            """CREATE TABLE IF NOT EXISTS closed_loop_runs (
               id TEXT PRIMARY KEY,
               gateway TEXT NOT NULL,
               version_id TEXT NOT NULL,
               source_proposal_id TEXT,
               action_kind TEXT,
               mode TEXT NOT NULL,
               before_status TEXT,
               after_status TEXT,
               decision TEXT NOT NULL,
               reason TEXT,
               result_json TEXT,
               metrics_before_json TEXT,
               metrics_after_json TEXT,
               created_at REAL NOT NULL
            )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_closed_loop_runs_version
               ON closed_loop_runs(version_id)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_closed_loop_runs_gateway
               ON closed_loop_runs(gateway, created_at)"""
        )

    db._execute_write(_do)


def _record_run(
    db: SessionDB,
    *,
    gateway: str,
    version_id: str,
    source_proposal_id: str | None,
    action_kind: str,
    mode: str,
    before_status: str,
    after_status: str,
    decision: str,
    reason: str,
    result: dict[str, Any] | None,
    metrics_before: dict[str, Any],
    metrics_after: dict[str, Any],
) -> str:
    rid = f"clr_{uuid.uuid4().hex[:12]}"
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO closed_loop_runs
               (id, gateway, version_id, source_proposal_id, action_kind, mode,
                before_status, after_status, decision, reason, result_json,
                metrics_before_json, metrics_after_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rid,
                gateway,
                version_id,
                source_proposal_id,
                action_kind,
                mode,
                before_status,
                after_status,
                decision,
                reason,
                json.dumps(result or {}, ensure_ascii=False, default=str),
                json.dumps(metrics_before, ensure_ascii=False, default=str),
                json.dumps(metrics_after, ensure_ascii=False, default=str),
                now,
            ),
        )

    db._execute_write(_do)
    return rid


def _metrics(db: SessionDB) -> dict[str, int]:
    return {
        "tasks_total": _q(db, "SELECT COUNT(*) FROM tasks"),
        "tasks_completed": _q(db, "SELECT COUNT(*) FROM tasks WHERE status='completed'"),
        "tasks_failed": _q(db, "SELECT COUNT(*) FROM tasks WHERE status='failed'"),
        "policy_evaluations": _q(db, "SELECT COUNT(*) FROM policy_evaluations"),
        "policy_needs_approval": _q(
            db,
            "SELECT COUNT(*) FROM policy_evaluations WHERE decision='allow_with_approval'",
        ),
        "skills_candidate": _q(db, "SELECT COUNT(*) FROM skill_registry WHERE status='candidate'"),
        "skills_active": _q(db, "SELECT COUNT(*) FROM skill_registry WHERE status='active'"),
        "precedents": _q(db, "SELECT COUNT(*) FROM precedent_records"),
        "doctrines_proposed": _q(
            db,
            "SELECT COUNT(*) FROM doctrine_registry WHERE status='proposed'",
        ),
    }


def _proposal(db: SessionDB, proposal_id: str | None) -> dict[str, Any]:
    if not proposal_id:
        return {}
    row = db._conn.execute(
        "SELECT * FROM capability_proposals WHERE id = ?",
        (proposal_id,),
    ).fetchone()
    return dict(row) if row else {}


def _incubating_versions(db: SessionDB, cap: int) -> list[dict[str, Any]]:
    rows = db._conn.execute(
        """SELECT * FROM capability_versions
           WHERE status = 'incubating'
                 AND source_proposal_id IS NOT NULL
           ORDER BY created_at ASC
           LIMIT ?""",
        (cap,),
    ).fetchall()
    return [dict(r) for r in rows]


def _action_hint(version: dict[str, Any]) -> dict[str, Any]:
    definition = _load_json(version.get("definition_json"))
    source = definition.get("source") or {}
    hint = source.get("action_hint") or {}
    return hint if isinstance(hint, dict) else {}


def _eligible(
    db: SessionDB,
    version: dict[str, Any],
    *,
    safe_kinds: set[str],
    max_risk: float,
    min_gain: float,
) -> tuple[bool, str, dict[str, Any], dict[str, Any]]:
    prop = _proposal(db, version.get("source_proposal_id"))
    hint = _action_hint(version)
    kind = str(hint.get("kind") or "")
    if not kind:
        return False, "missing_action_hint", prop, hint
    if kind not in safe_kinds:
        return False, f"unsafe_kind:{kind}", prop, hint

    risk = float(prop.get("risk_score") or 1.0)
    gain = float(prop.get("expected_gain") or 0.0)
    if risk > max_risk:
        return False, f"risk_score:{risk:.2f}>{max_risk:.2f}", prop, hint
    if gain < min_gain:
        return False, f"expected_gain:{gain:.2f}<{min_gain:.2f}", prop, hint

    return True, "eligible", prop, hint


def _process_version(
    db: SessionDB,
    *,
    gateway: str,
    version: dict[str, Any],
    prop: dict[str, Any],
    hint: dict[str, Any],
    apply: bool,
) -> dict[str, Any]:
    vid = version["id"]
    kind = str(hint.get("kind") or "")
    before = str(version.get("status") or "")
    metrics_before = _metrics(db)
    result: dict[str, Any] | None = None
    decision = "dry_run"
    reason = "eligible"
    after = before

    if not apply:
        return {
            "version_id": vid,
            "family": version.get("capability_family"),
            "source_proposal_id": version.get("source_proposal_id"),
            "kind": kind,
            "decision": decision,
            "reason": reason,
            "before_status": before,
            "after_status": after,
            "title": prop.get("title", ""),
        }

    _ensure_run_table(db)
    try:
        capability_manager.start_experiment(db, vid)
        result = capability_manager.execute_action(db, vid)
        if result.get("executed"):
            capability_manager.transition_status(
                db,
                vid,
                "limited_rollout",
                reason="closed_loop_controller_action_executed",
            )
            decision = "limited_rollout"
            reason = "action_executed"
        else:
            capability_manager.transition_status(
                db,
                vid,
                "deprecated",
                reason="closed_loop_controller_no_executable_action",
            )
            decision = "deprecated"
            reason = result.get("note") or "action_not_executed"
    except Exception as exc:
        decision = "deprecated"
        reason = f"execution_failed:{exc}"
        result = {"error": str(exc)}
        try:
            current = capability_manager.get_version(db, vid) or version
            if current.get("status") in ("incubating", "experimental"):
                capability_manager.transition_status(
                    db,
                    vid,
                    "deprecated",
                    reason="closed_loop_controller_execution_failed",
                )
        except Exception:
            pass

    current = capability_manager.get_version(db, vid) or version
    after = str(current.get("status") or before)
    metrics_after = _metrics(db)

    governance.review_proposal(
        db,
        subject_type="capability_version",
        subject_id=vid,
        risk_score=float(prop.get("risk_score") or 0.0),
        decision="approved" if decision == "limited_rollout" else "rejected",
        notes=(
            f"closed_loop_controller gateway={gateway} kind={kind} "
            f"decision={decision} reason={reason}"
        ),
        reviewer_id="closed_loop_controller",
    )
    run_id = _record_run(
        db,
        gateway=gateway,
        version_id=vid,
        source_proposal_id=version.get("source_proposal_id"),
        action_kind=kind,
        mode="apply",
        before_status=before,
        after_status=after,
        decision=decision,
        reason=reason,
        result=result,
        metrics_before=metrics_before,
        metrics_after=metrics_after,
    )

    return {
        "run_id": run_id,
        "version_id": vid,
        "family": version.get("capability_family"),
        "source_proposal_id": version.get("source_proposal_id"),
        "kind": kind,
        "decision": decision,
        "reason": reason,
        "before_status": before,
        "after_status": after,
        "title": prop.get("title", ""),
        "result": result,
    }


def process_gateway(
    name: str,
    path: Path,
    *,
    apply: bool,
    safe_kinds: set[str],
    max_risk: float,
    min_gain: float,
    cap: int,
) -> dict[str, Any]:
    if not path.exists():
        return {"gateway": name, "present": False, "skipped": "no_state_db"}

    db = SessionDB(path)
    versions = _incubating_versions(db, cap)
    processed: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for version in versions:
        ok, reason, prop, hint = _eligible(
            db,
            version,
            safe_kinds=safe_kinds,
            max_risk=max_risk,
            min_gain=min_gain,
        )
        if not ok:
            skipped.append({
                "version_id": version["id"],
                "family": str(version.get("capability_family") or ""),
                "reason": reason,
            })
            continue
        processed.append(
            _process_version(
                db,
                gateway=name,
                version=version,
                prop=prop,
                hint=hint,
                apply=apply,
            )
        )

    return {
        "gateway": name,
        "present": True,
        "mode": "apply" if apply else "dry_run",
        "incubating_scanned": len(versions),
        "processed": processed,
        "skipped": skipped,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    gateways = args.gateway or list(GATEWAYS.keys())
    safe_kinds = set(args.safe_kind or DEFAULT_SAFE_KINDS)
    results = {}
    for name in gateways:
        path = GATEWAYS[name]
        results[name] = process_gateway(
            name,
            path,
            apply=args.apply,
            safe_kinds=safe_kinds,
            max_risk=args.max_risk,
            min_gain=args.min_gain,
            cap=args.cap,
        )
    return {
        "timestamp": time.time(),
        "timestamp_iso": _now_iso(),
        "mode": "apply" if args.apply else "dry_run",
        "safe_kinds": sorted(safe_kinds),
        "thresholds": {
            "max_risk": args.max_risk,
            "min_gain": args.min_gain,
            "cap_per_gateway": args.cap,
        },
        "gateways": results,
    }


def format_text(snapshot: dict[str, Any]) -> str:
    lines = [
        f"Hermes Closed Loop Controller  {snapshot['timestamp_iso']}  ({snapshot['mode']})",
        "=" * 80,
        "safe_kinds=" + ",".join(snapshot["safe_kinds"]),
        (
            "thresholds="
            f"risk<={snapshot['thresholds']['max_risk']:.2f}, "
            f"gain>={snapshot['thresholds']['min_gain']:.2f}, "
            f"cap={snapshot['thresholds']['cap_per_gateway']}/gateway"
        ),
        "",
    ]
    for name, result in snapshot["gateways"].items():
        if not result.get("present"):
            lines.append(f"{name:8s} skipped: {result.get('skipped')}")
            continue
        processed = result.get("processed", [])
        skipped = result.get("skipped", [])
        lines.append(
            f"{name:8s} scanned={result['incubating_scanned']:>2d} "
            f"processed={len(processed):>2d} skipped={len(skipped):>2d}"
        )
        for item in processed:
            lines.append(
                "  "
                f"{item['version_id']} {item['before_status']}->{item['after_status']} "
                f"{item['kind']} decision={item['decision']} "
                f"family={item.get('family')}"
            )
        for item in skipped[:5]:
            lines.append(
                "  "
                f"skip {item['version_id']} family={item['family']} reason={item['reason']}"
            )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write transitions and execute safe action_hints")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON")
    parser.add_argument("--gateway", action="append", choices=sorted(GATEWAYS),
                        help="gateway to process; repeatable; default all")
    parser.add_argument("--safe-kind", action="append",
                        help="override safe action kind allowlist; repeatable")
    parser.add_argument("--max-risk", type=float, default=0.20)
    parser.add_argument("--min-gain", type=float, default=0.30)
    parser.add_argument("--cap", type=int, default=3,
                        help="max incubating versions scanned per gateway")
    args = parser.parse_args()

    snapshot = run(args)
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_text(snapshot))
    return 0


if __name__ == "__main__":
    sys.exit(main())
