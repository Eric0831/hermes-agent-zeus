"""Cultural Stability — detect and track cultural pathologies.

Monitors the system for signs of:
  - Verifier gaming: optimizing to pass verification without real substance
  - Bureaucratic drag: governance overhead slowing productive work
  - Mission dilution: avoiding hard tasks in favor of easy ones
  - Optimization collapse: over-fitting to narrow metrics
  - Excessive conservatism: rejecting all change to avoid risk

Drift events are tracked in cultural_drift_events and can be resolved
once mitigations are applied.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

PATHOLOGY_TYPES = (
    "verifier_gaming",
    "bureaucratic_drag",
    "mission_dilution",
    "optimization_collapse",
    "excessive_conservatism",
)


def _did() -> str:
    return f"drift_{uuid.uuid4().hex[:12]}"


# -- Detection Functions -----------------------------------------------------


def detect_verifier_gaming(
    db: Any,
    window_days: int = 30,
) -> Optional[dict]:
    """Detect if tasks are optimized to pass verifier without real substance.

    Signals:
    - High verification pass rate (> 90%) combined with
    - Low evidence diversity (few distinct tool types per task)

    Returns a pathology dict if detected, None otherwise.
    """
    cutoff = time.time() - window_days * 86400

    try:
        # Get completed tasks in window
        task_rows = db._conn.execute(
            """SELECT id FROM tasks
               WHERE status = 'completed' AND updated_at >= ?""",
            (cutoff,),
        ).fetchall()

        if not task_rows:
            return None

        task_ids = [r["id"] for r in task_rows]
        total_tasks = len(task_ids)

        # Get failed verification count
        failed_rows = db._conn.execute(
            """SELECT COUNT(DISTINCT task_id) as cnt FROM task_transitions
               WHERE to_state = 'failed' AND from_state = 'verifying'
               AND created_at >= ?""",
            (cutoff,),
        ).fetchall()
        failed_verifications = failed_rows[0]["cnt"] if failed_rows else 0

        # Pass rate
        verified_total = total_tasks + failed_verifications
        if verified_total == 0:
            return None

        pass_rate = total_tasks / verified_total

        # Evidence diversity: average distinct tool names per task
        if task_ids:
            placeholders = ", ".join("?" for _ in task_ids)
            ev_rows = db._conn.execute(
                f"""SELECT task_id, COUNT(DISTINCT tool_name) as tool_count
                    FROM evidence_records
                    WHERE task_id IN ({placeholders}) AND tool_name IS NOT NULL
                    GROUP BY task_id""",
                tuple(task_ids),
            ).fetchall()

            if ev_rows:
                avg_diversity = sum(r["tool_count"] for r in ev_rows) / len(ev_rows)
            else:
                avg_diversity = 0.0
        else:
            avg_diversity = 0.0

        # Signal: high pass rate + low diversity
        if pass_rate > 0.9 and avg_diversity < 2.0 and total_tasks >= 5:
            return {
                "pathology": "verifier_gaming",
                "severity": "medium" if pass_rate < 0.95 else "high",
                "signals": {
                    "pass_rate": round(pass_rate, 3),
                    "avg_tool_diversity": round(avg_diversity, 2),
                    "tasks_in_window": total_tasks,
                },
                "description": (
                    f"High verification pass rate ({pass_rate:.0%}) with low evidence "
                    f"diversity ({avg_diversity:.1f} tools/task) suggests verifier gaming"
                ),
            }
        return None
    except Exception as e:
        logger.error("[CulturalStability] detect_verifier_gaming failed: %s", e)
        return None


def detect_bureaucratic_drag(
    db: Any,
    window_days: int = 30,
) -> Optional[dict]:
    """Detect if governance overhead is slowing productive work.

    Signals:
    - High number of policy evaluations per task
    - Long deliberation times (open sessions exceeding normal duration)

    Returns a pathology dict if detected, None otherwise.
    """
    cutoff = time.time() - window_days * 86400

    try:
        # Policy evaluations per task
        eval_rows = db._conn.execute(
            """SELECT COUNT(*) as cnt FROM policy_evaluations
               WHERE created_at >= ?""",
            (cutoff,),
        ).fetchall()
        eval_count = eval_rows[0]["cnt"] if eval_rows else 0

        task_rows = db._conn.execute(
            """SELECT COUNT(*) as cnt FROM tasks
               WHERE created_at >= ?""",
            (cutoff,),
        ).fetchall()
        task_count = task_rows[0]["cnt"] if task_rows else 0

        evals_per_task = eval_count / max(task_count, 1)

        # Long-running deliberations
        open_delib = db._conn.execute(
            """SELECT COUNT(*) as cnt FROM deliberation_sessions
               WHERE status = 'open' AND started_at < ?""",
            (time.time() - 86400,),  # Open for more than 1 day
        ).fetchone()
        stale_deliberations = open_delib["cnt"] if open_delib else 0

        # Signal: high evaluations per task or stale deliberations
        if (evals_per_task > 5.0 and task_count >= 3) or stale_deliberations > 2:
            severity = "high" if evals_per_task > 10.0 else "medium"
            return {
                "pathology": "bureaucratic_drag",
                "severity": severity,
                "signals": {
                    "evals_per_task": round(evals_per_task, 2),
                    "total_evals": eval_count,
                    "total_tasks": task_count,
                    "stale_deliberations": stale_deliberations,
                },
                "description": (
                    f"Governance overhead detected: {evals_per_task:.1f} policy evaluations "
                    f"per task, {stale_deliberations} stale deliberations"
                ),
            }
        return None
    except Exception as e:
        logger.error("[CulturalStability] detect_bureaucratic_drag failed: %s", e)
        return None


def detect_mission_dilution(
    db: Any,
    window_days: int = 30,
) -> Optional[dict]:
    """Detect if the system is avoiding hard tasks in favor of easy ones.

    Signals:
    - Task type distribution shifting toward simpler types
    - Declining risk_level in recent tasks compared to earlier tasks

    Returns a pathology dict if detected, None otherwise.
    """
    cutoff = time.time() - window_days * 86400
    earlier_cutoff = cutoff - window_days * 86400  # Previous window

    try:
        # Recent risk distribution
        recent_rows = db._conn.execute(
            """SELECT risk_level, COUNT(*) as cnt FROM tasks
               WHERE created_at >= ?
               GROUP BY risk_level""",
            (cutoff,),
        ).fetchall()
        recent_risks = {r["risk_level"]: r["cnt"] for r in recent_rows}
        recent_total = sum(recent_risks.values())

        # Earlier risk distribution
        earlier_rows = db._conn.execute(
            """SELECT risk_level, COUNT(*) as cnt FROM tasks
               WHERE created_at >= ? AND created_at < ?
               GROUP BY risk_level""",
            (earlier_cutoff, cutoff),
        ).fetchall()
        earlier_risks = {r["risk_level"]: r["cnt"] for r in earlier_rows}
        earlier_total = sum(earlier_risks.values())

        if recent_total < 3 or earlier_total < 3:
            return None

        # Compute risk complexity index: low=1, medium=2, high=3
        risk_weights = {"low": 1, "medium": 2, "high": 3}
        recent_complexity = sum(
            risk_weights.get(k, 1) * v for k, v in recent_risks.items()
        ) / recent_total
        earlier_complexity = sum(
            risk_weights.get(k, 1) * v for k, v in earlier_risks.items()
        ) / earlier_total

        # Signal: significant decline in average complexity
        complexity_drop = earlier_complexity - recent_complexity
        if complexity_drop > 0.5:
            return {
                "pathology": "mission_dilution",
                "severity": "high" if complexity_drop > 1.0 else "medium",
                "signals": {
                    "recent_complexity": round(recent_complexity, 2),
                    "earlier_complexity": round(earlier_complexity, 2),
                    "complexity_drop": round(complexity_drop, 2),
                    "recent_risk_dist": recent_risks,
                    "earlier_risk_dist": earlier_risks,
                },
                "description": (
                    f"Task complexity declining: avg risk dropped from "
                    f"{earlier_complexity:.2f} to {recent_complexity:.2f} "
                    f"(delta={complexity_drop:.2f})"
                ),
            }
        return None
    except Exception as e:
        logger.error("[CulturalStability] detect_mission_dilution failed: %s", e)
        return None


# -- Aggregate Analysis ------------------------------------------------------


def analyze_culture(db: Any, *, window_days: int = 30) -> dict:
    """Run all pathology detectors and return a cultural health assessment.

    Returns:
        {
            "pathologies": [...],
            "drift_events": [...],
            "health_score": float (0-1),
            "recommendations": [...]
        }
    """
    pathologies = []
    recs = []

    # Run all detectors
    gaming = detect_verifier_gaming(db, window_days)
    if gaming:
        pathologies.append(gaming)
        recs.append("Increase evidence diversity requirements for verification")

    drag = detect_bureaucratic_drag(db, window_days)
    if drag:
        pathologies.append(drag)
        recs.append("Simplify governance process — reduce policy evaluations per task")

    dilution = detect_mission_dilution(db, window_days)
    if dilution:
        pathologies.append(dilution)
        recs.append("Rebalance task allocation toward higher-complexity work")

    # Get active drift events
    drift_events = get_drift_events(db, status="detected", limit=20)

    # Compute health score: starts at 1.0, reduced by pathologies
    severity_weights = {"low": 0.1, "medium": 0.2, "high": 0.35}
    penalty = sum(
        severity_weights.get(p.get("severity", "medium"), 0.2)
        for p in pathologies
    )
    # Also penalize unresolved drift events
    penalty += len(drift_events) * 0.05
    health_score = max(0.0, min(1.0, 1.0 - penalty))

    return {
        "pathologies": pathologies,
        "drift_events": drift_events,
        "health_score": round(health_score, 3),
        "recommendations": recs,
    }


# -- Drift Event Management --------------------------------------------------


def record_drift_event(
    db: Any,
    drift_type: str,
    severity: str,
    signals: dict | str,
) -> str:
    """Record a cultural drift event.

    Returns the drift event id.
    """
    did = _did()
    now = time.time()
    signals_json = (
        json.dumps(signals, ensure_ascii=False)
        if not isinstance(signals, str)
        else signals
    )

    def _do(conn):
        conn.execute(
            """INSERT INTO cultural_drift_events
               (id, drift_type, severity, signals_json, status, detected_at, resolved_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (did, drift_type, severity, signals_json, "detected", now, None),
        )

    db._execute_write(_do)
    logger.info(
        "[CulturalStability] Drift event %s: type=%s severity=%s",
        did, drift_type, severity,
    )
    return did


def get_drift_events(
    db: Any,
    status: str = "detected",
    limit: int = 20,
) -> list[dict]:
    """Get cultural drift events filtered by status."""
    try:
        rows = db._conn.execute(
            """SELECT * FROM cultural_drift_events
               WHERE status = ?
               ORDER BY detected_at DESC LIMIT ?""",
            (status, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[CulturalStability] get_drift_events failed: %s", e)
        return []
