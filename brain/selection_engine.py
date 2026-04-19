"""Selection Engine — governed selection for evolution units.

Combines fitness scores, governance decisions, and continuity checks into
final adoption decisions. Implements threshold-based selection with
governance override capabilities.

Decision logic:
  - adopt:  delta > ADOPT_THRESHOLD and governance=pass and continuity=ok
  - trial:  delta > TRIAL_THRESHOLD and governance=conditional
  - reject: governance=fail or score below baseline
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Selection Thresholds ─────────────────────────────────────────

ADOPT_THRESHOLD = 0.05
TRIAL_THRESHOLD = 0.02


def _decision_id() -> str:
    return f"sel_{uuid.uuid4().hex[:12]}"


# ── Selection Evaluation ─────────────────────────────────────────


def evaluate_selection(
    db: Any,
    unit_id: str,
    fitness_run_id: str,
    *,
    governance_pass: bool = True,
    governance_conditional: bool = False,
    continuity_ok: bool = True,
    baseline_score: Optional[float] = None,
) -> dict:
    """Apply selection rules based on fitness, governance, and continuity.

    Returns {decision_id, decision, decision_reason, delta_score}.
    """
    # Fetch fitness score
    fit_row = db._conn.execute(
        "SELECT score FROM fitness_runs WHERE id = ?", (fitness_run_id,)
    ).fetchone()
    score = fit_row["score"] if fit_row else 0.0

    # Determine baseline
    if baseline_score is None:
        baseline_score = 0.5  # neutral default

    delta = score - baseline_score

    # Decision logic
    if not governance_pass and not governance_conditional:
        decision = "reject"
        reason = f"Governance failed. Score={score:.4f}, delta={delta:.4f}"
    elif score < baseline_score and delta < -ADOPT_THRESHOLD:
        decision = "reject"
        reason = f"Score {score:.4f} below baseline {baseline_score:.4f} by {abs(delta):.4f}"
    elif delta >= ADOPT_THRESHOLD and governance_pass and continuity_ok:
        decision = "adopt"
        reason = (
            f"Delta {delta:.4f} >= {ADOPT_THRESHOLD} threshold, "
            f"governance=pass, continuity=ok"
        )
    elif delta >= TRIAL_THRESHOLD and governance_conditional:
        decision = "trial"
        reason = (
            f"Delta {delta:.4f} >= {TRIAL_THRESHOLD} trial threshold, "
            f"governance=conditional"
        )
    elif delta >= TRIAL_THRESHOLD and governance_pass and not continuity_ok:
        decision = "trial"
        reason = (
            f"Delta {delta:.4f} >= {TRIAL_THRESHOLD}, governance=pass "
            f"but continuity check failed — trial only"
        )
    else:
        decision = "reject"
        reason = (
            f"Delta {delta:.4f} below thresholds "
            f"(adopt={ADOPT_THRESHOLD}, trial={TRIAL_THRESHOLD})"
        )

    did = _decision_id()
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO selection_decisions
               (id, unit_id, fitness_run_id, decision, decision_reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (did, unit_id, fitness_run_id, decision, reason, now),
        )

    db._execute_write(_do)
    logger.info(
        "Selection %s for unit %s: %s (delta=%.4f)",
        did, unit_id, decision, delta,
    )
    return {
        "decision_id": did,
        "decision": decision,
        "decision_reason": reason,
        "delta_score": delta,
    }


# ── Decision Queries ─────────────────────────────────────────────


def get_decision(db: Any, decision_id: str) -> Optional[dict]:
    """Fetch a single selection decision."""
    row = db._conn.execute(
        "SELECT * FROM selection_decisions WHERE id = ?", (decision_id,)
    ).fetchone()
    return dict(row) if row else None


def get_decisions_for_unit(db: Any, unit_id: str) -> list[dict]:
    """Get all selection decisions for a unit, newest first."""
    rows = db._conn.execute(
        """SELECT * FROM selection_decisions
           WHERE unit_id = ? ORDER BY created_at DESC""",
        (unit_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Status Updates ───────────────────────────────────────────────


def adopt_unit(db: Any, unit_id: str) -> None:
    """Set unit status to 'adopted'."""
    now = time.time()

    def _do(conn):
        conn.execute(
            "UPDATE evolution_units SET status = 'adopted', updated_at = ? WHERE id = ?",
            (now, unit_id),
        )

    db._execute_write(_do)
    logger.info("Adopted unit %s", unit_id)


def reject_unit(db: Any, unit_id: str, reason: str = "") -> None:
    """Set unit status to 'rejected'."""
    now = time.time()

    def _do(conn):
        conn.execute(
            "UPDATE evolution_units SET status = 'rejected', updated_at = ? WHERE id = ?",
            (now, unit_id),
        )

    db._execute_write(_do)
    logger.info("Rejected unit %s: %s", unit_id, reason or "(no reason)")


def retire_unit(db: Any, unit_id: str, reason: str = "") -> None:
    """Set unit status to 'retired'."""
    now = time.time()

    def _do(conn):
        conn.execute(
            "UPDATE evolution_units SET status = 'retired', updated_at = ? WHERE id = ?",
            (now, unit_id),
        )

    db._execute_write(_do)
    logger.info("Retired unit %s: %s", unit_id, reason or "(no reason)")
