"""Incubator — test capabilities in isolation before rollout.

Runs replay, simulation, or synthetic tests against a candidate
capability version. Compares baseline vs candidate metrics to produce
a promotion recommendation: 'promote', 'revise', or 'discard'.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

RUN_TYPES = ("replay", "simulation", "synthetic")
RUN_STATUSES = ("pending", "running", "completed", "failed")


def _run_id() -> str:
    return f"incr_{uuid.uuid4().hex[:12]}"


# ── Public API ────────────────────────────────────────────────────


def create_run(
    db: Any,
    capability_version_id: str,
    run_type: str,
    scope: dict,
) -> str:
    """Create an incubator run record.

    Args:
        capability_version_id: the capability version under test
        run_type: 'replay', 'simulation', or 'synthetic'
        scope: dict describing the test scope (task_family, window, etc.)

    Returns the run id.
    """
    if run_type not in RUN_TYPES:
        raise ValueError(f"Invalid run_type: {run_type}")

    rid = _run_id()
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO incubator_runs
               (id, capability_version_id, run_type, scope_json,
                status, baseline_metrics_json, candidate_metrics_json,
                summary_json, started_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rid,
                capability_version_id,
                run_type,
                json.dumps(scope, ensure_ascii=False, default=str),
                "pending",
                None,
                None,
                None,
                now,
                None,
            ),
        )

    db._execute_write(_do)
    logger.info(
        "[Incubator] Created run %s (type=%s, capability=%s)",
        rid, run_type, capability_version_id,
    )
    return rid


def evaluate_run(
    db: Any,
    run_id: str,
    *,
    baseline_tasks: Optional[list[dict]] = None,
    candidate_tasks: Optional[list[dict]] = None,
) -> dict:
    """Evaluate an incubator run by comparing baseline vs candidate.

    If baseline_tasks / candidate_tasks are not provided, the function
    queries the DB using the scope stored in the run record.

    Returns:
        {
            status: str,
            baseline_metrics: dict,
            candidate_metrics: dict,
            gain_delta: dict,
            recommendation: 'promote' | 'revise' | 'discard',
        }
    """
    run = get_run(db, run_id)
    if not run:
        raise ValueError(f"Incubator run not found: {run_id}")

    # Mark as running
    _update_status(db, run_id, "running")

    # Load tasks from DB if not provided
    if baseline_tasks is None or candidate_tasks is None:
        scope = _parse_json(run.get("scope_json", "{}"))
        task_family = scope.get("task_family", "")
        window_days = scope.get("window_days", 30)

        if baseline_tasks is None:
            baseline_tasks = _query_tasks(db, task_family, window_days)
        if candidate_tasks is None:
            candidate_tasks = baseline_tasks  # same data, different eval

    baseline_metrics = _compute_metrics(baseline_tasks)
    candidate_metrics = _compute_metrics(candidate_tasks)
    gain_delta = _compute_gain_delta(baseline_metrics, candidate_metrics)
    recommendation = _decide_recommendation(gain_delta)

    # Persist metrics
    now = time.time()

    def _do(conn):
        conn.execute(
            """UPDATE incubator_runs
               SET status = 'completed',
                   baseline_metrics_json = ?,
                   candidate_metrics_json = ?,
                   completed_at = ?
               WHERE id = ?""",
            (
                json.dumps(baseline_metrics, ensure_ascii=False, default=str),
                json.dumps(candidate_metrics, ensure_ascii=False, default=str),
                now,
                run_id,
            ),
        )

    db._execute_write(_do)

    result = {
        "status": "completed",
        "baseline_metrics": baseline_metrics,
        "candidate_metrics": candidate_metrics,
        "gain_delta": gain_delta,
        "recommendation": recommendation,
    }

    logger.info(
        "[Incubator] Run %s evaluated: recommendation=%s",
        run_id, recommendation,
    )
    return result


def get_run(db: Any, run_id: str) -> Optional[dict]:
    """Get an incubator run by ID."""
    row = db._conn.execute(
        "SELECT * FROM incubator_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    return dict(row) if row else None


def get_runs_for_capability(
    db: Any,
    capability_version_id: str,
) -> list[dict]:
    """Get all incubator runs for a capability version."""
    rows = db._conn.execute(
        """SELECT * FROM incubator_runs
           WHERE capability_version_id = ?
           ORDER BY started_at DESC""",
        (capability_version_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def complete_run(db: Any, run_id: str, summary: dict) -> None:
    """Finalize an incubator run with a summary."""
    now = time.time()

    def _do(conn):
        conn.execute(
            """UPDATE incubator_runs
               SET status = 'completed',
                   summary_json = ?,
                   completed_at = ?
               WHERE id = ?""",
            (
                json.dumps(summary, ensure_ascii=False, default=str),
                now,
                run_id,
            ),
        )

    db._execute_write(_do)
    logger.info("[Incubator] Run %s completed", run_id)


# ── Metrics Computation ──────────────────────────────────────────


def _compute_metrics(tasks: list[dict]) -> dict:
    """Compute aggregate metrics from a set of tasks."""
    if not tasks:
        return {
            "completion_rate": 0.0,
            "verification_pass_rate": 0.0,
            "avg_evidence_count": 0.0,
            "avg_duration": 0.0,
            "task_count": 0,
        }

    total = len(tasks)
    completed = sum(1 for t in tasks if t.get("status") == "completed")
    verified = [t for t in tasks if t.get("verification_status")]
    passed = sum(
        1 for t in verified if t["verification_status"] == "pass"
    )

    # Evidence count — use evidence_count field or default to 0
    evidence_counts = [t.get("evidence_count", 0) for t in tasks]
    avg_evidence = sum(evidence_counts) / total if total else 0.0

    # Duration
    durations = []
    for t in tasks:
        started = t.get("started_at")
        ended = t.get("completed_at")
        if started and ended:
            try:
                d = float(ended) - float(started)
                if d > 0:
                    durations.append(d)
            except (TypeError, ValueError):
                pass

    avg_duration = sum(durations) / len(durations) if durations else 0.0

    return {
        "completion_rate": completed / total,
        "verification_pass_rate": passed / len(verified) if verified else 0.0,
        "avg_evidence_count": avg_evidence,
        "avg_duration": avg_duration,
        "task_count": total,
    }


def _compute_gain_delta(
    baseline: dict,
    candidate: dict,
) -> dict:
    """Compute the delta between candidate and baseline metrics."""
    keys = ("completion_rate", "verification_pass_rate",
            "avg_evidence_count", "avg_duration")
    delta = {}
    for k in keys:
        b = baseline.get(k, 0.0)
        c = candidate.get(k, 0.0)
        # For avg_duration, lower is better so we invert
        if k == "avg_duration":
            delta[k] = b - c  # positive means candidate is faster
        else:
            delta[k] = c - b  # positive means candidate is better
    return delta


def _decide_recommendation(gain_delta: dict) -> str:
    """Decide promotion recommendation from gain deltas.

    - 'promote': net positive gains across key metrics
    - 'revise': mixed results, needs iteration
    - 'discard': net negative, candidate is worse
    """
    cr = gain_delta.get("completion_rate", 0.0)
    vr = gain_delta.get("verification_pass_rate", 0.0)

    # Weighted score: completion rate matters most
    score = cr * 0.5 + vr * 0.3 + gain_delta.get("avg_duration", 0.0) * 0.2

    if score > 0.05:
        return "promote"
    elif score < -0.05:
        return "discard"
    else:
        return "revise"


# ── Internal Helpers ─────────────────────────────────────────────


def _update_status(db: Any, run_id: str, status: str) -> None:
    """Update run status."""
    def _do(conn):
        conn.execute(
            "UPDATE incubator_runs SET status = ? WHERE id = ?",
            (status, run_id),
        )
    db._execute_write(_do)


def _query_tasks(
    db: Any,
    task_family: str,
    window_days: int,
) -> list[dict]:
    """Query tasks from the DB for a given family and time window."""
    cutoff = time.time() - (window_days * 86400)
    try:
        if task_family:
            rows = db._conn.execute(
                """SELECT id, task_type, status, verification_status,
                          retry_count, started_at, completed_at, created_at
                   FROM tasks
                   WHERE task_type = ? AND created_at >= ?
                   ORDER BY created_at""",
                (task_family, cutoff),
            ).fetchall()
        else:
            rows = db._conn.execute(
                """SELECT id, task_type, status, verification_status,
                          retry_count, started_at, completed_at, created_at
                   FROM tasks
                   WHERE created_at >= ?
                   ORDER BY created_at""",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _parse_json(raw: str) -> dict:
    """Safely parse a JSON string."""
    try:
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}
