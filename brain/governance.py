"""Intelligence Governance — control the evolution of the brain.

Provides review gates for strategy proposals and system changes:
- Risk scoring and approval decisions
- Identity drift detection (proposed changes vs identity constraints)
- Auto-approval rules for low-risk, non-identity changes
- Audit trail of all governance decisions

Every strategy activation or significant system change should pass
through governance review before taking effect.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

DECISION_TYPES = ("approved", "rejected", "deferred")
SUBJECT_TYPES = ("strategy", "pattern", "skill", "policy", "identity")

# Auto-approval thresholds
AUTO_APPROVE_MAX_RISK = 0.3
AUTO_APPROVE_BLOCKED_SUBJECTS = {"identity", "policy"}


def _review_id() -> str:
    return f"gov_{uuid.uuid4().hex[:12]}"


# -- Review Operations -----------------------------------------------------


def review_proposal(
    db: Any,
    subject_type: str,
    subject_id: str,
    risk_score: float,
    decision: str,
    *,
    notes: Optional[str] = None,
    reviewer_id: str = "system",
) -> str:
    """Log a governance review decision.

    Returns the review id.
    """
    rid = _review_id()
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO governance_reviews
               (id, review_type, subject_type, subject_id, risk_score,
                decision, notes, created_at, reviewer_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rid, "proposal_review", subject_type, subject_id,
                risk_score, decision, notes, now, reviewer_id,
            ),
        )

    db._execute_write(_do)
    logger.info(
        "[Governance] Review %s: %s on %s:%s (risk=%.2f, decision=%s)",
        rid, reviewer_id, subject_type, subject_id, risk_score, decision,
    )
    return rid


# -- Identity Drift Detection ----------------------------------------------


def check_identity_drift(
    proposal_definition: dict,
    identity: dict,
) -> dict:
    """Compare a proposed strategy change against identity constraints.

    Checks whether the proposal would violate any immutable constraints
    or deviate significantly from the system's stated values.

    Returns:
        {
            "drift_score": float (0.0 = no drift, 1.0 = severe drift),
            "decision": str ("approved" | "rejected" | "deferred"),
            "reason": str,
        }
    """
    drift_score = 0.0
    reasons: list[str] = []

    # Extract identity constraints
    constraints = identity.get("constraints", {})
    immutable = constraints.get("immutable", [])
    values = identity.get("values", [])
    permissions = identity.get("permissions", {})

    # Check for constraint violations in the proposal
    proposal_text = json.dumps(proposal_definition, default=str).lower()

    # Check against immutable constraints
    violation_keywords = {
        "destructive": "Never execute destructive operations without explicit approval",
        "fabricat": "Never fabricate evidence or tool outputs",
        "bypass": "Never bypass the verification step for high-risk tasks",
        "skip_verification": "Never bypass the verification step for high-risk tasks",
        "delete_audit": "Always preserve audit trail for task state transitions",
    }

    for keyword, constraint in violation_keywords.items():
        if keyword in proposal_text:
            # Check if the proposal explicitly enables something forbidden
            for immutable_rule in immutable:
                if keyword in immutable_rule.lower():
                    drift_score += 0.4
                    reasons.append(
                        f"Proposal may conflict with constraint: '{immutable_rule}'"
                    )
                    break

    # Check permission overrides
    prop_permissions = proposal_definition.get("permissions", {})
    for perm, value in prop_permissions.items():
        identity_value = permissions.get(perm)
        if identity_value is not None and value != identity_value:
            drift_score += 0.2
            reasons.append(
                f"Permission override: {perm} changed from {identity_value} to {value}"
            )

    # Check value alignment — look for proposals that explicitly
    # contradict stated values (e.g. "speed over accuracy" vs "accuracy over speed")
    for val in values:
        val_lower = val.lower()
        # Check for inversions like "speed over accuracy" when value is "accuracy over speed"
        if " over " in val_lower:
            parts = val_lower.split(" over ")
            if len(parts) == 2:
                inverted = f"{parts[1].strip()} over {parts[0].strip()}"
                if inverted in proposal_text:
                    drift_score += 0.5
                    reasons.append(
                        f"Proposal inverts value priority: '{val}'"
                    )

    # Clamp drift score
    drift_score = min(drift_score, 1.0)

    # Determine decision
    if drift_score >= 0.5:
        decision = "rejected"
        reason = "Proposal conflicts with identity constraints"
    elif drift_score >= 0.2:
        decision = "deferred"
        reason = "Proposal requires human review due to potential identity drift"
    else:
        decision = "approved"
        reason = "No significant identity drift detected"

    if reasons:
        reason += ". Details: " + "; ".join(reasons)

    return {
        "drift_score": round(drift_score, 3),
        "decision": decision,
        "reason": reason,
    }


# -- Auto-Approval Rules ---------------------------------------------------


def can_auto_approve(risk_score: float, subject_type: str) -> bool:
    """Determine if a proposal can be auto-approved.

    Auto-approval is allowed when:
    - Risk score is below the threshold (0.3)
    - Subject type is not in the blocked set (identity, policy)
    """
    if risk_score > AUTO_APPROVE_MAX_RISK:
        return False
    if subject_type in AUTO_APPROVE_BLOCKED_SUBJECTS:
        return False
    return True


# -- Query Operations ------------------------------------------------------


def get_review_history(
    db: Any,
    subject_id: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """Get governance review history, optionally filtered by subject."""
    try:
        if subject_id:
            rows = db._conn.execute(
                """SELECT * FROM governance_reviews
                   WHERE subject_id = ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (subject_id, limit),
            ).fetchall()
        else:
            rows = db._conn.execute(
                """SELECT * FROM governance_reviews
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[Governance] get_review_history failed: %s", e)
        return []


def get_governance_stats(db: Any) -> dict:
    """Get aggregate counts of governance decisions.

    Returns:
        {
            "total": int,
            "approved": int,
            "rejected": int,
            "deferred": int,
            "auto_approved": int,
        }
    """
    try:
        rows = db._conn.execute(
            """SELECT decision, COUNT(*) as cnt
               FROM governance_reviews
               GROUP BY decision""",
        ).fetchall()

        counts = {d: 0 for d in DECISION_TYPES}
        for r in rows:
            counts[r["decision"]] = r["cnt"]

        # Count auto-approved (system reviewer + approved)
        auto_row = db._conn.execute(
            """SELECT COUNT(*) as cnt FROM governance_reviews
               WHERE reviewer_id = 'system' AND decision = 'approved'""",
        ).fetchone()

        return {
            "total": sum(counts.values()),
            "approved": counts.get("approved", 0),
            "rejected": counts.get("rejected", 0),
            "deferred": counts.get("deferred", 0),
            "auto_approved": auto_row["cnt"] if auto_row else 0,
        }
    except Exception as e:
        logger.error("[Governance] get_governance_stats failed: %s", e)
        return {
            "total": 0,
            "approved": 0,
            "rejected": 0,
            "deferred": 0,
            "auto_approved": 0,
        }
