"""Multilevel Selection — individual vs group fitness weighting.

Balances individual unit fitness against group-level fitness to prevent
selfish optimization that harms the collective.  The alpha parameter
controls the group-vs-individual weighting and shifts toward group
priority as system criticality increases.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── ID Generation ───────────────────────────────────────────────


def _group_run_id() -> str:
    return f"grp_{uuid.uuid4().hex[:12]}"


def _multilevel_run_id() -> str:
    return f"mls_{uuid.uuid4().hex[:12]}"


# ── Alpha Recommendation ───────────────────────────────────────

_ALPHA_MAP = {
    "stable": 0.35,
    "elevated": 0.60,
    "critical": 0.80,
}


def get_recommended_alpha(criticality_status: str = "stable") -> float:
    """Return recommended alpha (group weight) for a criticality level.

    stable   → 0.35  (moderate group weight)
    elevated → 0.60  (higher group weight)
    critical → 0.80  (strongly group-oriented)
    """
    return _ALPHA_MAP.get(criticality_status, 0.35)


# ── Group Fitness ───────────────────────────────────────────────


def calculate_group_fitness(
    db: Any,
    family: str,
    scope_type: str,
    scope_id: str,
    window_start: float,
    window_end: float,
) -> dict:
    """Compute group-level fitness for a family within a scope.

    Metrics:
      - civilization_health: from cultural_stability if available (else 0.5)
      - task_completion_rate: completed / total tasks for this family
      - doctrine_consistency: ratio of governed units that passed governance

    Score = avg(civilization_health, task_completion_rate, doctrine_consistency)
    """
    metrics: dict[str, Any] = {}
    estimated_fields: list[str] = []

    # 1. Civilization health — try cultural_stability analysis
    try:
        from brain.cultural_stability import analyze_culture
        culture = analyze_culture(db)
        civ_health = culture.get("health_score", 0.5)
    except Exception:
        civ_health = 0.5
        estimated_fields.append("civilization_health")
    metrics["civilization_health"] = civ_health

    # 2. Task completion rate for the family
    tasks = db._conn.execute(
        """SELECT status FROM tasks
           WHERE created_at >= ? AND created_at <= ?
             AND task_type = ?""",
        (window_start, window_end, family),
    ).fetchall()

    if tasks:
        total = len(tasks)
        completed = sum(1 for t in tasks if dict(t).get("status") == "completed")
        task_rate = completed / total
    else:
        task_rate = 0.5
        estimated_fields.append("task_completion_rate")
    metrics["task_completion_rate"] = task_rate

    # 3. Doctrine consistency — ratio of adopted vs total evolution units
    eu_rows = db._conn.execute(
        """SELECT status FROM evolution_units
           WHERE family = ?
             AND created_at >= ? AND created_at <= ?""",
        (family, window_start, window_end),
    ).fetchall()

    if eu_rows:
        all_units = [dict(r) for r in eu_rows]
        if all_units:
            adopted = sum(1 for u in all_units if u["status"] == "adopted")
            doctrine = adopted / len(all_units) if len(all_units) > 0 else 0.5
        else:
            doctrine = 0.5
            estimated_fields.append("doctrine_consistency")
    else:
        doctrine = 0.5
        estimated_fields.append("doctrine_consistency")
    metrics["doctrine_consistency"] = doctrine

    metrics["estimated"] = estimated_fields

    score = round(
        (civ_health + task_rate + doctrine) / 3.0, 6
    )

    # Persist
    rid = _group_run_id()
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO group_fitness_runs
               (id, family, scope_type, scope_id, metrics_json, score,
                window_start, window_end, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rid, family, scope_type, scope_id,
                json.dumps(metrics, ensure_ascii=False),
                score, window_start, window_end, now,
            ),
        )

    db._execute_write(_do)
    logger.info(
        "Group fitness %s: family=%s scope=%s/%s score=%.4f",
        rid, family, scope_type, scope_id, score,
    )
    return {"run_id": rid, "score": score, "metrics": metrics}


# ── Multilevel Score ────────────────────────────────────────────


def calculate_multilevel_score(
    db: Any,
    unit_id: str,
    individual_run_id: str,
    group_run_id: str,
    alpha: float = 0.35,
) -> dict:
    """Compute multilevel selection score.

    W_total = (1 - alpha) * W_ind + alpha * W_grp

    Stores result in multilevel_selection_runs and returns
    {run_id, individual_score, group_score, alpha, total_score}.
    """
    # Fetch individual fitness
    ind_row = db._conn.execute(
        "SELECT score FROM fitness_runs WHERE id = ?",
        (individual_run_id,),
    ).fetchone()
    ind_score = dict(ind_row)["score"] if ind_row else 0.0

    # Fetch group fitness
    grp_row = db._conn.execute(
        "SELECT score FROM group_fitness_runs WHERE id = ?",
        (group_run_id,),
    ).fetchone()
    grp_score = dict(grp_row)["score"] if grp_row else 0.0

    total = round((1 - alpha) * ind_score + alpha * grp_score, 6)

    rid = _multilevel_run_id()
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO multilevel_selection_runs
               (id, unit_id, individual_fitness_run_id, group_fitness_run_id,
                alpha, total_score, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (rid, unit_id, individual_run_id, group_run_id, alpha, total, now),
        )

    db._execute_write(_do)
    logger.info(
        "Multilevel %s: unit=%s ind=%.4f grp=%.4f alpha=%.2f total=%.4f",
        rid, unit_id, ind_score, grp_score, alpha, total,
    )
    return {
        "run_id": rid,
        "individual_score": ind_score,
        "group_score": grp_score,
        "alpha": alpha,
        "total_score": total,
    }


# ── History ─────────────────────────────────────────────────────


def get_multilevel_history(
    db: Any,
    unit_id: str,
    limit: int = 10,
) -> list[dict]:
    """Fetch recent multilevel selection runs for a unit."""
    rows = db._conn.execute(
        """SELECT * FROM multilevel_selection_runs
           WHERE unit_id = ?
           ORDER BY created_at DESC LIMIT ?""",
        (unit_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]
