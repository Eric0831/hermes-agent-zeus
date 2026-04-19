"""Experiment Engine — lifecycle management for evolution experiments.

Manages the full lifecycle of experiments that test evolution unit changes:
  - create -> start -> evaluate -> complete/rollback

Experiment types:
  - offline_replay:   re-run historical tasks with new unit
  - sandbox_trial:    isolated environment test
  - shadow:           parallel execution without affecting production
  - canary:           limited production traffic
  - bounded_rollout:  phased production deployment with kill switch
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────

EXPERIMENT_TYPES = (
    "offline_replay",
    "sandbox_trial",
    "shadow",
    "canary",
    "bounded_rollout",
)

EXPERIMENT_STATUSES = (
    "created",
    "running",
    "won",
    "lost",
    "inconclusive",
    "rolled_back",
)


def _experiment_id() -> str:
    return f"exp_{uuid.uuid4().hex[:12]}"


# ── Experiment Lifecycle ─────────────────────────────────────────


def create_experiment(
    db: Any,
    experiment_type: str,
    unit_id: str,
    scope: dict,
    *,
    baseline_unit_id: Optional[str] = None,
) -> str:
    """Create an experiment with status='created'. Returns experiment_id."""
    eid = _experiment_id()
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO evolution_experiments
               (id, experiment_type, unit_id, baseline_unit_id, scope_json,
                status, metrics_json, started_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                eid, experiment_type, unit_id, baseline_unit_id,
                json.dumps(scope, ensure_ascii=False),
                "created", None, now, None,
            ),
        )

    db._execute_write(_do)
    logger.info(
        "Created experiment %s [%s] for unit %s (baseline=%s)",
        eid, experiment_type, unit_id, baseline_unit_id,
    )
    return eid


def start_experiment(db: Any, experiment_id: str) -> None:
    """Set experiment status to 'running'."""
    now = time.time()

    def _do(conn):
        conn.execute(
            """UPDATE evolution_experiments
               SET status = 'running', started_at = ?
               WHERE id = ?""",
            (now, experiment_id),
        )

    db._execute_write(_do)
    logger.info("Started experiment %s", experiment_id)


def evaluate_experiment(db: Any, experiment_id: str) -> dict:
    """Compare unit vs baseline metrics from fitness_runs.

    Returns {status, quality_delta, verifier_delta, cost_delta,
             recommendation: 'promote'|'revise'|'discard'|'inconclusive'}.
    """
    exp = get_experiment(db, experiment_id)
    if not exp:
        return {
            "status": "not_found",
            "quality_delta": 0.0,
            "verifier_delta": 0.0,
            "cost_delta": 0.0,
            "recommendation": "inconclusive",
        }

    unit_id = exp["unit_id"]
    baseline_id = exp.get("baseline_unit_id")

    # Get latest fitness for the experiment unit
    unit_fit = db._conn.execute(
        """SELECT score, metrics_json FROM fitness_runs
           WHERE unit_id = ? ORDER BY created_at DESC LIMIT 1""",
        (unit_id,),
    ).fetchone()

    unit_score = unit_fit["score"] if unit_fit else 0.5
    unit_metrics = json.loads(unit_fit["metrics_json"]) if unit_fit else {}

    # Get latest fitness for the baseline unit
    if baseline_id:
        base_fit = db._conn.execute(
            """SELECT score, metrics_json FROM fitness_runs
               WHERE unit_id = ? ORDER BY created_at DESC LIMIT 1""",
            (baseline_id,),
        ).fetchone()
        base_score = base_fit["score"] if base_fit else 0.5
        base_metrics = json.loads(base_fit["metrics_json"]) if base_fit else {}
    else:
        base_score = 0.5
        base_metrics = {}

    quality_delta = unit_metrics.get("quality", 0.5) - base_metrics.get("quality", 0.5)
    verifier_delta = unit_metrics.get("verification", 0.5) - base_metrics.get("verification", 0.5)
    cost_delta = unit_metrics.get("cost", 0.5) - base_metrics.get("cost", 0.5)
    overall_delta = unit_score - base_score

    # Recommendation logic
    if overall_delta > 0.05 and quality_delta >= 0:
        recommendation = "promote"
    elif overall_delta > 0 and quality_delta < 0:
        recommendation = "revise"
    elif overall_delta < -0.05:
        recommendation = "discard"
    else:
        recommendation = "inconclusive"

    result = {
        "status": exp["status"],
        "quality_delta": quality_delta,
        "verifier_delta": verifier_delta,
        "cost_delta": cost_delta,
        "overall_delta": overall_delta,
        "recommendation": recommendation,
    }

    logger.info(
        "Evaluated experiment %s: recommendation=%s (delta=%.4f)",
        experiment_id, recommendation, overall_delta,
    )
    return result


def complete_experiment(
    db: Any,
    experiment_id: str,
    result: str,
    metrics: Optional[dict] = None,
) -> None:
    """Set experiment status to result ('won'|'lost'|'inconclusive')."""
    now = time.time()

    def _do(conn):
        conn.execute(
            """UPDATE evolution_experiments
               SET status = ?, metrics_json = ?, completed_at = ?
               WHERE id = ?""",
            (
                result,
                json.dumps(metrics, ensure_ascii=False) if metrics else None,
                now, experiment_id,
            ),
        )

    db._execute_write(_do)
    logger.info("Completed experiment %s with result=%s", experiment_id, result)


def rollback_experiment(
    db: Any,
    experiment_id: str,
    reason: str = "",
) -> None:
    """Set experiment status to 'rolled_back' and revert unit status."""
    exp = get_experiment(db, experiment_id)
    now = time.time()

    def _do(conn):
        conn.execute(
            """UPDATE evolution_experiments
               SET status = 'rolled_back', completed_at = ?,
                   metrics_json = ?
               WHERE id = ?""",
            (
                now,
                json.dumps({"rollback_reason": reason}, ensure_ascii=False),
                experiment_id,
            ),
        )
        # Revert the unit to candidate status if experiment exists
        if exp:
            conn.execute(
                """UPDATE evolution_units
                   SET status = 'candidate', updated_at = ?
                   WHERE id = ?""",
                (now, exp["unit_id"]),
            )

    db._execute_write(_do)
    logger.info(
        "Rolled back experiment %s: %s", experiment_id, reason or "(no reason)",
    )


# ── Queries ──────────────────────────────────────────────────────


def get_experiment(db: Any, experiment_id: str) -> Optional[dict]:
    """Fetch a single experiment by ID."""
    row = db._conn.execute(
        "SELECT * FROM evolution_experiments WHERE id = ?", (experiment_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["scope"] = json.loads(d.pop("scope_json", "{}"))
    if d.get("metrics_json"):
        d["metrics"] = json.loads(d.pop("metrics_json"))
    else:
        d.pop("metrics_json", None)
        d["metrics"] = None
    return d


def get_experiments(
    db: Any,
    unit_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """Query experiments with optional filters."""
    conditions: list[str] = []
    params: list[Any] = []

    if unit_id is not None:
        conditions.append("unit_id = ?")
        params.append(unit_id)
    if status is not None:
        conditions.append("status = ?")
        params.append(status)

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    rows = db._conn.execute(
        f"""SELECT * FROM evolution_experiments{where}
            ORDER BY started_at DESC LIMIT ?""",
        params,
    ).fetchall()

    results = []
    for row in rows:
        d = dict(row)
        d["scope"] = json.loads(d.pop("scope_json", "{}"))
        if d.get("metrics_json"):
            d["metrics"] = json.loads(d.pop("metrics_json"))
        else:
            d.pop("metrics_json", None)
            d["metrics"] = None
        results.append(d)
    return results
