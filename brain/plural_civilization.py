"""Plural Civilization — interface for interacting with external civilizations.

Manages the registry of external civilizations, trust/risk scoring,
and treaty lifecycle (proposal, ratification, termination).

Treaty types:
  - bounded_cooperation: limited joint work within agreed boundaries
  - protocol_translation: bridge between incompatible communication protocols
  - non_interference: agreement to avoid impacting each other's operations
  - controlled_exchange: structured data/resource exchange with safeguards
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

TREATY_TYPES = (
    "bounded_cooperation",
    "protocol_translation",
    "non_interference",
    "controlled_exchange",
)


def _cid() -> str:
    return f"civ_{uuid.uuid4().hex[:12]}"


def _tid() -> str:
    return f"trty_{uuid.uuid4().hex[:12]}"


# -- Civilization Registry ---------------------------------------------------


def register_civilization(
    db: Any,
    name: str,
    profile: dict | str,
) -> str:
    """Register an external civilization.

    Returns the civilization id.
    """
    civ_id = _cid()
    now = time.time()
    profile_json = (
        json.dumps(profile, ensure_ascii=False)
        if not isinstance(profile, str) else profile
    )

    def _do(conn):
        conn.execute(
            """INSERT INTO external_civilizations
               (id, name, profile_json, trust_score, risk_score,
                status, created_at, updated_at)
               VALUES (?, ?, ?, 0.5, 0.5, 'observed', ?, ?)""",
            (civ_id, name, profile_json, now, now),
        )

    db._execute_write(_do)
    logger.info("[PluralCiv] Registered civilization %s: %s", civ_id, name)
    return civ_id


def get_civilization(db: Any, civ_id: str) -> Optional[dict]:
    """Get a single civilization by ID."""
    try:
        row = db._conn.execute(
            "SELECT * FROM external_civilizations WHERE id = ?", (civ_id,),
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("[PluralCiv] get_civilization failed: %s", e)
        return None


def get_all_civilizations(
    db: Any,
    status: Optional[str] = None,
) -> list[dict]:
    """Get all civilizations, optionally filtered by status."""
    try:
        if status:
            rows = db._conn.execute(
                """SELECT * FROM external_civilizations
                   WHERE status = ?
                   ORDER BY created_at DESC""",
                (status,),
            ).fetchall()
        else:
            rows = db._conn.execute(
                "SELECT * FROM external_civilizations ORDER BY created_at DESC",
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[PluralCiv] get_all_civilizations failed: %s", e)
        return []


def update_trust(
    db: Any,
    civ_id: str,
    trust_delta: float,
    risk_delta: float = 0,
) -> dict:
    """Adjust trust and risk scores for a civilization (clamped 0-1).

    Returns the updated {trust_score, risk_score}.
    """
    now = time.time()

    # Read current scores
    try:
        row = db._conn.execute(
            "SELECT trust_score, risk_score FROM external_civilizations WHERE id = ?",
            (civ_id,),
        ).fetchone()
    except Exception as e:
        logger.error("[PluralCiv] update_trust read failed: %s", e)
        return {"trust_score": 0.5, "risk_score": 0.5}

    if not row:
        logger.warning("[PluralCiv] Civilization %s not found", civ_id)
        return {"trust_score": 0.5, "risk_score": 0.5}

    current = dict(row)
    new_trust = max(0.0, min(1.0, current["trust_score"] + trust_delta))
    new_risk = max(0.0, min(1.0, current["risk_score"] + risk_delta))

    def _do(conn):
        conn.execute(
            """UPDATE external_civilizations
               SET trust_score = ?, risk_score = ?, updated_at = ?
               WHERE id = ?""",
            (new_trust, new_risk, now, civ_id),
        )

    db._execute_write(_do)
    logger.info(
        "[PluralCiv] Updated %s: trust=%.3f risk=%.3f",
        civ_id, new_trust, new_risk,
    )
    return {"trust_score": new_trust, "risk_score": new_risk}


# -- Treaty Management ------------------------------------------------------


def propose_treaty(
    db: Any,
    civ_id: str,
    treaty_type: str,
    definition: dict | str,
    risk_assessment: Optional[dict] = None,
) -> str:
    """Propose a treaty with an external civilization.

    Returns the treaty id.
    """
    treaty_id = _tid()
    now = time.time()
    def_json = (
        json.dumps(definition, ensure_ascii=False)
        if not isinstance(definition, str) else definition
    )
    risk_json = (
        json.dumps(risk_assessment, ensure_ascii=False)
        if risk_assessment else None
    )

    def _do(conn):
        conn.execute(
            """INSERT INTO treaties
               (id, external_civ_id, treaty_type, definition_json,
                risk_json, status, created_at, ratified_at, terminated_at)
               VALUES (?, ?, ?, ?, ?, 'proposed', ?, NULL, NULL)""",
            (treaty_id, civ_id, treaty_type, def_json, risk_json, now),
        )

    db._execute_write(_do)
    logger.info(
        "[PluralCiv] Proposed treaty %s: type=%s civ=%s",
        treaty_id, treaty_type, civ_id,
    )
    return treaty_id


def ratify_treaty(db: Any, treaty_id: str) -> bool:
    """Ratify a proposed treaty. Returns True if successful."""
    now = time.time()

    # Verify treaty exists and is in proposed state
    try:
        row = db._conn.execute(
            "SELECT status FROM treaties WHERE id = ?", (treaty_id,),
        ).fetchone()
        if not row or dict(row)["status"] != "proposed":
            logger.warning(
                "[PluralCiv] Cannot ratify treaty %s: not in proposed state",
                treaty_id,
            )
            return False
    except Exception as e:
        logger.error("[PluralCiv] ratify_treaty check failed: %s", e)
        return False

    def _do(conn):
        conn.execute(
            """UPDATE treaties
               SET status = 'ratified', ratified_at = ?
               WHERE id = ?""",
            (now, treaty_id),
        )

    db._execute_write(_do)
    logger.info("[PluralCiv] Ratified treaty %s", treaty_id)
    return True


def terminate_treaty(
    db: Any,
    treaty_id: str,
    reason: str = "",
) -> None:
    """Terminate a treaty."""
    now = time.time()

    def _do(conn):
        conn.execute(
            """UPDATE treaties
               SET status = 'terminated', terminated_at = ?
               WHERE id = ?""",
            (now, treaty_id),
        )

    db._execute_write(_do)
    logger.info("[PluralCiv] Terminated treaty %s: %s", treaty_id, reason)


def get_treaties(
    db: Any,
    civ_id: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    """Get treaties, optionally filtered by civilization and/or status."""
    try:
        conditions = []
        params: list = []

        if civ_id:
            conditions.append("external_civ_id = ?")
            params.append(civ_id)
        if status:
            conditions.append("status = ?")
            params.append(status)

        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = db._conn.execute(
            f"SELECT * FROM treaties{where} ORDER BY created_at DESC",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[PluralCiv] get_treaties failed: %s", e)
        return []


# -- Pure Risk Assessment ----------------------------------------------------


def assess_treaty_risk(
    treaty_definition: dict,
    civ_profile: dict,
) -> dict:
    """Assess the risk of a treaty given the civilization's profile.

    Pure function — no DB access.

    Returns:
        {risk_score, risk_factors, recommendation}
    """
    risk_factors: list[str] = []
    risk_score = 0.0

    # Check for broad scope (more risk)
    scope = treaty_definition.get("scope", [])
    if len(scope) > 3:
        risk_factors.append("broad_scope: treaty covers many areas")
        risk_score += 0.2

    # Check for data sharing provisions
    if treaty_definition.get("data_sharing"):
        risk_factors.append("data_sharing: treaty involves data exchange")
        risk_score += 0.15

    # Check civilization trust indicators
    civ_trust = civ_profile.get("trust_indicators", {})
    if civ_trust.get("history_of_violations"):
        risk_factors.append("civ_violations: civilization has past violations")
        risk_score += 0.3

    # Check for asymmetric obligations
    our_obligations = len(treaty_definition.get("our_obligations", []))
    their_obligations = len(treaty_definition.get("their_obligations", []))
    if our_obligations > 0 and their_obligations == 0:
        risk_factors.append("asymmetric_obligations: one-sided obligations")
        risk_score += 0.25

    # Check unknown civilization factors
    if not civ_profile.get("verified"):
        risk_factors.append("unverified_civilization: profile not verified")
        risk_score += 0.1

    risk_score = min(1.0, risk_score)

    # Recommendation thresholds
    if risk_score >= 0.6:
        recommendation = "block"
    elif risk_score >= 0.3:
        recommendation = "caution"
    else:
        recommendation = "proceed"

    return {
        "risk_score": round(risk_score, 3),
        "risk_factors": risk_factors,
        "recommendation": recommendation,
    }
