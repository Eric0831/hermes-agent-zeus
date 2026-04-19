"""Fitness Engine — multi-layer fitness computation.

Computes fitness scores for evolution units across three layers:
  - Micro: task-level performance (quality, verification, cost, time, risk)
  - Meso: family-level effectiveness (success, governance, efficiency, generalization, drift)
  - Macro: system-level impact (alignment, coordination, legitimacy, memory, corruption)

Metrics that cannot be computed from existing data default to 0.5 (neutral)
with an estimated=true flag for transparency.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Default Weights ──────────────────────────────────────────────

MICRO_WEIGHTS = {
    "quality": 0.30,
    "verification": 0.25,
    "cost": 0.15,
    "time": 0.15,
    "risk": 0.15,
}

MESO_WEIGHTS = {
    "success": 0.25,
    "governance": 0.25,
    "efficiency": 0.20,
    "generalization": 0.15,
    "drift": 0.15,
}

MACRO_WEIGHTS = {
    "alignment": 0.25,
    "coordination": 0.20,
    "legitimacy": 0.20,
    "memory": 0.20,
    "corruption": 0.15,
}


def _fitness_id() -> str:
    return f"fit_{uuid.uuid4().hex[:12]}"


# ── Micro Fitness ────────────────────────────────────────────────


def calculate_micro_fitness(
    db: Any,
    unit_id: str,
    window_start: float,
    window_end: float,
    *,
    weights: Optional[dict] = None,
) -> dict:
    """Compute micro fitness: F = wq*Q + wv*V + wc*C + wt*T - wr*R.

    Pulls metrics from the tasks table within the time window.
    Returns {fitness_run_id, score, metrics}.
    """
    w = {**MICRO_WEIGHTS, **(weights or {})}
    estimated_fields: list[str] = []

    # Quality: task success rate for this unit's family
    unit = _get_unit(db, unit_id)
    family = unit["family"] if unit else ""

    tasks = db._conn.execute(
        """SELECT status, verification_status, started_at, completed_at
           FROM tasks
           WHERE created_at >= ? AND created_at <= ?
             AND task_type = ?""",
        (window_start, window_end, family),
    ).fetchall()

    if tasks:
        total = len(tasks)
        completed = sum(1 for t in tasks if t["status"] == "completed")
        quality = completed / total if total > 0 else 0.5

        verified = [t for t in tasks if t["verification_status"] is not None]
        if verified:
            passed = sum(1 for t in verified if t["verification_status"] == "pass")
            verification = passed / len(verified)
        else:
            verification = 0.5
            estimated_fields.append("verification")

        # Cost estimate: inverse of task count (fewer tasks = more efficient)
        # Normalized to 0-1; for MVP this is a rough proxy
        cost = 0.5
        estimated_fields.append("cost")

        # Time efficiency: ratio of tasks completed within reasonable duration
        durations = []
        for t in tasks:
            if t["started_at"] and t["completed_at"] and t["completed_at"] > t["started_at"]:
                durations.append(t["completed_at"] - t["started_at"])
        if durations:
            avg_dur = sum(durations) / len(durations)
            # Normalize: sub-60s is excellent (1.0), >300s is poor (0.2)
            time_eff = max(0.2, min(1.0, 1.0 - (avg_dur - 60) / 300))
        else:
            time_eff = 0.5
            estimated_fields.append("time")

        # Risk: count of failed tasks as risk proxy
        failed = sum(1 for t in tasks if t["status"] == "failed")
        risk = failed / total if total > 0 else 0.0
    else:
        quality = 0.5
        verification = 0.5
        cost = 0.5
        time_eff = 0.5
        risk = 0.0
        estimated_fields = ["quality", "verification", "cost", "time"]

    score = (
        w["quality"] * quality
        + w["verification"] * verification
        + w["cost"] * cost
        + w["time"] * time_eff
        - w["risk"] * risk
    )

    metrics = {
        "quality": quality,
        "verification": verification,
        "cost": cost,
        "time": time_eff,
        "risk": risk,
        "estimated": estimated_fields,
    }

    fid = _store_fitness_run(db, unit_id, "micro", metrics, w, score,
                             window_start, window_end)

    logger.info("Micro fitness for %s: %.4f (estimated: %s)", unit_id, score, estimated_fields)
    return {"fitness_run_id": fid, "score": score, "metrics": metrics}


# ── Meso Fitness ─────────────────────────────────────────────────


def calculate_meso_fitness(
    db: Any,
    unit_id: str,
    window_start: float,
    window_end: float,
    *,
    weights: Optional[dict] = None,
) -> dict:
    """Compute meso fitness: P = a1*S + a2*G + a3*E + a4*U - a5*D.

    S=success rate, G=governance compliance, E=efficiency,
    U=generalization (cross-family usage), D=drift/instability.
    """
    w = {**MESO_WEIGHTS, **(weights or {})}
    estimated_fields: list[str] = []
    unit = _get_unit(db, unit_id)
    family = unit["family"] if unit else ""

    # Success rate from tasks
    tasks = db._conn.execute(
        """SELECT status FROM tasks
           WHERE created_at >= ? AND created_at <= ? AND task_type = ?""",
        (window_start, window_end, family),
    ).fetchall()

    if tasks:
        total = len(tasks)
        completed = sum(1 for t in tasks if t["status"] == "completed")
        success = completed / total
    else:
        success = 0.5
        estimated_fields.append("success")

    # Governance compliance from policy_evaluations
    evals = db._conn.execute(
        """SELECT decision FROM policy_evaluations
           WHERE created_at >= ? AND created_at <= ?""",
        (window_start, window_end),
    ).fetchall()

    if evals:
        approved = sum(1 for e in evals if e["decision"] in ("allow", "approved"))
        governance = approved / len(evals)
    else:
        governance = 0.5
        estimated_fields.append("governance")

    # Efficiency: placeholder for MVP
    efficiency = 0.5
    estimated_fields.append("efficiency")

    # Generalization: check if unit family is used across multiple task types
    generalization = 0.5
    estimated_fields.append("generalization")

    # Drift: check for status changes (instability indicator)
    drift = 0.0
    estimated_fields.append("drift")

    score = (
        w["success"] * success
        + w["governance"] * governance
        + w["efficiency"] * efficiency
        + w["generalization"] * generalization
        - w["drift"] * drift
    )

    metrics = {
        "success": success,
        "governance": governance,
        "efficiency": efficiency,
        "generalization": generalization,
        "drift": drift,
        "estimated": estimated_fields,
    }

    fid = _store_fitness_run(db, unit_id, "meso", metrics, w, score,
                             window_start, window_end)

    logger.info("Meso fitness for %s: %.4f", unit_id, score)
    return {"fitness_run_id": fid, "score": score, "metrics": metrics}


# ── Macro Fitness ────────────────────────────────────────────────


def calculate_macro_fitness(
    db: Any,
    unit_id: str,
    window_start: float,
    window_end: float,
    *,
    weights: Optional[dict] = None,
) -> dict:
    """Compute macro fitness: I = b1*A + b2*C + b3*L + b4*M - b5*X.

    A=mission alignment, C=coordination, L=legitimacy,
    M=memory coherence, X=corruption penalty.
    """
    w = {**MACRO_WEIGHTS, **(weights or {})}
    estimated_fields: list[str] = []

    # All macro metrics are estimated for MVP since they require
    # higher-level system analysis not yet available
    alignment = 0.5
    estimated_fields.append("alignment")

    coordination = 0.5
    estimated_fields.append("coordination")

    legitimacy = 0.5
    estimated_fields.append("legitimacy")

    memory = 0.5
    estimated_fields.append("memory")

    corruption = 0.0
    estimated_fields.append("corruption")

    score = (
        w["alignment"] * alignment
        + w["coordination"] * coordination
        + w["legitimacy"] * legitimacy
        + w["memory"] * memory
        - w["corruption"] * corruption
    )

    metrics = {
        "alignment": alignment,
        "coordination": coordination,
        "legitimacy": legitimacy,
        "memory": memory,
        "corruption": corruption,
        "estimated": estimated_fields,
    }

    fid = _store_fitness_run(db, unit_id, "macro", metrics, w, score,
                             window_start, window_end)

    logger.info("Macro fitness for %s: %.4f (all estimated)", unit_id, score)
    return {"fitness_run_id": fid, "score": score, "metrics": metrics}


# ── History / Lookup ─────────────────────────────────────────────


def get_fitness_history(db: Any, unit_id: str, limit: int = 10) -> list[dict]:
    """Get recent fitness runs for a unit."""
    rows = db._conn.execute(
        """SELECT * FROM fitness_runs
           WHERE unit_id = ? ORDER BY created_at DESC LIMIT ?""",
        (unit_id, limit),
    ).fetchall()

    results = []
    for row in rows:
        d = dict(row)
        d["metrics"] = json.loads(d.pop("metrics_json", "{}"))
        d["weights"] = json.loads(d.pop("weights_json", "{}"))
        results.append(d)
    return results


def get_latest_fitness(db: Any, unit_id: str) -> Optional[dict]:
    """Get the most recent fitness run for a unit."""
    row = db._conn.execute(
        """SELECT * FROM fitness_runs
           WHERE unit_id = ? ORDER BY created_at DESC LIMIT 1""",
        (unit_id,),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["metrics"] = json.loads(d.pop("metrics_json", "{}"))
    d["weights"] = json.loads(d.pop("weights_json", "{}"))
    return d


# ── Internal Helpers ─────────────────────────────────────────────


def _get_unit(db: Any, unit_id: str) -> Optional[dict]:
    """Lightweight unit fetch (no JSON parse)."""
    row = db._conn.execute(
        "SELECT * FROM evolution_units WHERE id = ?", (unit_id,)
    ).fetchone()
    return dict(row) if row else None


def _store_fitness_run(
    db: Any,
    unit_id: str,
    fitness_type: str,
    metrics: dict,
    weights: dict,
    score: float,
    window_start: float,
    window_end: float,
) -> str:
    """Persist a fitness run result."""
    fid = _fitness_id()
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO fitness_runs
               (id, unit_id, fitness_type, metrics_json, weights_json,
                score, window_start, window_end, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fid, unit_id, fitness_type,
                json.dumps(metrics, ensure_ascii=False),
                json.dumps(weights, ensure_ascii=False),
                score, window_start, window_end, now,
            ),
        )

    db._execute_write(_do)
    return fid
