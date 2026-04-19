"""Brain Metrics — structured observability for the AgentEOS brain.

Computes task-level KPIs from the tasks/evidence/transitions tables.
Designed to be queried by /status and future Grafana/Prometheus exporters.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


def get_brain_metrics(
    db: Any,
    session_id: Optional[str] = None,
    *,
    window_seconds: Optional[float] = None,
) -> dict[str, Any]:
    """
    Compute brain metrics, optionally scoped to a session and time window.

    Returns:
    {
        "tasks": {
            "total": int,
            "completed": int,
            "failed": int,
            "active": int,
            "cancelled": int,
            "completion_rate": float,  # completed / (completed + failed)
        },
        "verification": {
            "pass": int,
            "fail_retriable": int,
            "fail_non_retriable": int,
            "pass_rate": float,
        },
        "evidence": {
            "total_records": int,
            "avg_per_task": float,
            "tools_used": [...],
        },
        "timing": {
            "avg_duration_s": float,
            "p50_duration_s": float,
            "fastest_s": float,
            "slowest_s": float,
        },
        "retries": {
            "total_retries": int,
            "tasks_with_retries": int,
        },
        "policy": {
            "total_evaluations": int,
            "denied": int,
            "approval_required": int,
        },
        "computed_at": float,
    }
    """
    metrics: dict[str, Any] = {"computed_at": time.time()}

    if db is None:
        return metrics

    try:
        where, params = _build_where(session_id, window_seconds)
        metrics["tasks"] = _task_metrics(db, where, params)
        metrics["verification"] = _verification_metrics(db, where, params)
        metrics["evidence"] = _evidence_metrics(db, where, params)
        metrics["timing"] = _timing_metrics(db, where, params)
        metrics["retries"] = _retry_metrics(db, where, params)
        metrics["policy"] = _policy_metrics(db, where, params)
    except Exception as e:
        logger.debug("Metrics computation error (non-fatal): %s", e)

    return metrics


def format_metrics_text(metrics: dict[str, Any]) -> str:
    """Format metrics as a human-readable text block for /status."""
    parts = []

    tasks = metrics.get("tasks", {})
    if tasks.get("total", 0) > 0:
        parts.append(
            f"Tasks: {tasks['total']} total | "
            f"{tasks.get('completed', 0)} done | "
            f"{tasks.get('failed', 0)} failed | "
            f"{tasks.get('active', 0)} active"
        )
        cr = tasks.get("completion_rate")
        if cr is not None:
            parts.append(f"Completion rate: {cr:.0%}")

    verification = metrics.get("verification", {})
    vr = verification.get("pass_rate")
    if vr is not None and verification.get("pass", 0) + verification.get("fail_retriable", 0) > 0:
        parts.append(f"Verification pass rate: {vr:.0%}")

    evidence = metrics.get("evidence", {})
    if evidence.get("total_records", 0) > 0:
        parts.append(
            f"Evidence: {evidence['total_records']} records "
            f"(avg {evidence.get('avg_per_task', 0):.1f}/task)"
        )

    timing = metrics.get("timing", {})
    avg = timing.get("avg_duration_s")
    if avg is not None and avg > 0:
        parts.append(f"Avg task duration: {avg:.1f}s")

    retries = metrics.get("retries", {})
    if retries.get("total_retries", 0) > 0:
        parts.append(f"Retries: {retries['total_retries']} across {retries['tasks_with_retries']} tasks")

    policy = metrics.get("policy", {})
    if policy.get("denied", 0) > 0:
        parts.append(f"Policy: {policy['denied']} denied, {policy.get('approval_required', 0)} required approval")

    if not parts:
        return "(no brain activity yet)"

    return "\n".join(parts)


# ── Internal Queries ──────────────────────────────────────────────


def _build_where(
    session_id: Optional[str],
    window_seconds: Optional[float],
) -> tuple[str, list]:
    """Build WHERE clause fragments."""
    conditions = []
    params: list[Any] = []

    if session_id:
        conditions.append("t.session_id = ?")
        params.append(session_id)

    if window_seconds:
        conditions.append("t.created_at >= ?")
        params.append(time.time() - window_seconds)

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    return where, params


def _task_metrics(db: Any, where: str, params: list) -> dict:
    rows = db._conn.execute(
        f"SELECT status, COUNT(*) as cnt FROM tasks t {where} GROUP BY status",
        params,
    ).fetchall()

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = r["cnt"]

    total = sum(counts.values())
    completed = counts.get("completed", 0)
    failed = counts.get("failed", 0)
    cancelled = counts.get("cancelled", 0)
    active = total - completed - failed - cancelled

    denominator = completed + failed
    rate = completed / denominator if denominator > 0 else None

    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "active": active,
        "cancelled": cancelled,
        "completion_rate": rate,
    }


def _verification_metrics(db: Any, where: str, params: list) -> dict:
    rows = db._conn.execute(
        f"""SELECT verification_status, COUNT(*) as cnt
            FROM tasks t {where}
            {"AND" if where else "WHERE"} verification_status IS NOT NULL
            GROUP BY verification_status""",
        params,
    ).fetchall()

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verification_status"]] = r["cnt"]

    passed = counts.get("pass", 0)
    failed_r = counts.get("fail_retriable", 0)
    failed_nr = counts.get("fail_non_retriable", 0)
    total_verified = passed + failed_r + failed_nr
    rate = passed / total_verified if total_verified > 0 else None

    return {
        "pass": passed,
        "fail_retriable": failed_r,
        "fail_non_retriable": failed_nr,
        "pass_rate": rate,
    }


def _evidence_metrics(db: Any, where: str, params: list) -> dict:
    # Total evidence records for tasks matching the filter
    row = db._conn.execute(
        f"""SELECT COUNT(*) as cnt
            FROM evidence_records e
            JOIN tasks t ON e.task_id = t.id
            {where}""",
        params,
    ).fetchone()
    total = row["cnt"] if row else 0

    # Count tasks with evidence
    row2 = db._conn.execute(
        f"""SELECT COUNT(DISTINCT e.task_id) as cnt
            FROM evidence_records e
            JOIN tasks t ON e.task_id = t.id
            {where}""",
        params,
    ).fetchone()
    tasks_with = row2["cnt"] if row2 else 0

    avg = total / tasks_with if tasks_with > 0 else 0.0

    # Distinct tools used
    tools_rows = db._conn.execute(
        f"""SELECT DISTINCT e.tool_name
            FROM evidence_records e
            JOIN tasks t ON e.task_id = t.id
            {where}
            AND e.tool_name IS NOT NULL""",
        params,
    ).fetchall()
    tools = sorted(r["tool_name"] for r in tools_rows)

    return {
        "total_records": total,
        "avg_per_task": avg,
        "tools_used": tools,
    }


def _timing_metrics(db: Any, where: str, params: list) -> dict:
    rows = db._conn.execute(
        f"""SELECT (completed_at - started_at) as duration
            FROM tasks t
            {where}
            {"AND" if where else "WHERE"} started_at IS NOT NULL
            AND completed_at IS NOT NULL
            AND completed_at > started_at
            ORDER BY duration""",
        params,
    ).fetchall()

    if not rows:
        return {"avg_duration_s": None, "p50_duration_s": None,
                "fastest_s": None, "slowest_s": None}

    durations = [r["duration"] for r in rows]
    n = len(durations)

    return {
        "avg_duration_s": sum(durations) / n,
        "p50_duration_s": durations[n // 2],
        "fastest_s": durations[0],
        "slowest_s": durations[-1],
    }


def _retry_metrics(db: Any, where: str, params: list) -> dict:
    row = db._conn.execute(
        f"""SELECT SUM(retry_count) as total, COUNT(*) as cnt
            FROM tasks t
            {where}
            {"AND" if where else "WHERE"} retry_count > 0""",
        params,
    ).fetchone()

    return {
        "total_retries": row["total"] or 0 if row else 0,
        "tasks_with_retries": row["cnt"] or 0 if row else 0,
    }


def _policy_metrics(db: Any, where: str, params: list) -> dict:
    # Policy metrics use a different join pattern — subquery filters tasks
    if where:
        pol_where = "WHERE pe.task_id IN (SELECT id FROM tasks t " + where + ")"
        pol_params = params
    else:
        pol_where = ""
        pol_params = []

    rows = db._conn.execute(
        f"""SELECT decision, COUNT(*) as cnt
            FROM policy_evaluations pe
            {pol_where}
            GROUP BY decision""",
        pol_params,
    ).fetchall()

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["decision"]] = r["cnt"]

    return {
        "total_evaluations": sum(counts.values()),
        "denied": counts.get("deny", 0),
        "approval_required": counts.get("allow_with_approval", 0),
    }
