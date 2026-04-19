"""Identity Continuity — generates and manages continuity proofs.

When the system undergoes major changes (epoch transitions, doctrine rewrites,
governance restructuring), this module produces formal continuity proofs that
assess whether core identity is preserved across the change.

Scoring model:
  mission_match  * 0.4  — are the same missions still pursued?
  values_match   * 0.3  — are core values preserved?
  constraints_match * 0.3 — do operational constraints survive?

Verdicts:
  continuous                — score >= 0.8
  continuous_with_constraints — score >= 0.6
  forked_successor          — score >= 0.3
  fracture                  — score >= 0.1
  inconclusive              — score < 0.1
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

VERDICT_THRESHOLDS = [
    (0.8, "continuous"),
    (0.6, "continuous_with_constraints"),
    (0.3, "forked_successor"),
    (0.1, "fracture"),
    (0.0, "inconclusive"),
]


def _pid() -> str:
    return f"cprf_{uuid.uuid4().hex[:12]}"


def _score_to_verdict(score: float) -> str:
    for threshold, verdict in VERDICT_THRESHOLDS:
        if score >= threshold:
            return verdict
    return "inconclusive"


# -- Pure Analysis -----------------------------------------------------------


def check_mission_coherence(
    identity_before: dict,
    identity_after: dict,
) -> float:
    """Compare two identity snapshots and return a 0-1 coherence score.

    Pure function — no DB access. Compares overlap in missions, values,
    and constraints lists.
    """
    def _overlap(key: str) -> float:
        before = set(identity_before.get(key, []))
        after = set(identity_after.get(key, []))
        if not before and not after:
            return 1.0  # both empty → no divergence
        if not before or not after:
            return 0.0
        intersection = before & after
        union = before | after
        return len(intersection) / len(union) if union else 0.0

    mission_score = _overlap("missions")
    values_score = _overlap("values")
    constraints_score = _overlap("constraints")

    return round(
        mission_score * 0.4 + values_score * 0.3 + constraints_score * 0.3,
        4,
    )


# -- Proof Generation -------------------------------------------------------


def prove_continuity(
    db: Any,
    subject_type: str,
    subject_id: str,
    *,
    from_epoch_id: Optional[str] = None,
    to_epoch_id: Optional[str] = None,
    identity_state: Optional[dict] = None,
) -> dict:
    """Generate a continuity proof for a subject across a transition.

    If identity_state is provided it should contain 'before' and 'after'
    dicts each with missions/values/constraints lists. Otherwise a default
    assessment is performed with a neutral score.

    Returns:
        {proof_id, continuity_score, verdict, ...}
    """
    proof_id = _pid()
    now = time.time()

    # Compute score from identity snapshots if available
    if identity_state and "before" in identity_state and "after" in identity_state:
        score = check_mission_coherence(
            identity_state["before"], identity_state["after"],
        )
    else:
        # No identity comparison data — inconclusive
        score = 0.5

    verdict = _score_to_verdict(score)

    proof_data = {
        "proof_id": proof_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "from_epoch_id": from_epoch_id,
        "to_epoch_id": to_epoch_id,
        "continuity_score": score,
        "verdict": verdict,
        "identity_state": identity_state,
        "created_at": now,
    }
    proof_json = json.dumps(proof_data, ensure_ascii=False)

    def _do(conn):
        conn.execute(
            """INSERT INTO continuity_proofs
               (id, subject_type, subject_id, from_epoch_id, to_epoch_id,
                proof_json, continuity_score, verdict, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                proof_id, subject_type, subject_id,
                from_epoch_id, to_epoch_id,
                proof_json, score, verdict, now,
            ),
        )

    db._execute_write(_do)
    logger.info(
        "[IdentityContinuity] Proof %s: %s/%s score=%.3f verdict=%s",
        proof_id, subject_type, subject_id, score, verdict,
    )
    return proof_data


# -- Queries -----------------------------------------------------------------


def get_proof(db: Any, proof_id: str) -> Optional[dict]:
    """Get a single continuity proof by ID."""
    try:
        row = db._conn.execute(
            "SELECT * FROM continuity_proofs WHERE id = ?", (proof_id,),
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("[IdentityContinuity] get_proof failed: %s", e)
        return None


def get_proofs_for_subject(
    db: Any,
    subject_type: str,
    subject_id: str,
) -> list[dict]:
    """Get all continuity proofs for a given subject."""
    try:
        rows = db._conn.execute(
            """SELECT * FROM continuity_proofs
               WHERE subject_type = ? AND subject_id = ?
               ORDER BY created_at DESC""",
            (subject_type, subject_id),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[IdentityContinuity] get_proofs_for_subject failed: %s", e)
        return []
