"""Civilization Planner — long-horizon planning for system continuity.

Assesses institutional health, identifies fragilities, and proposes
reforms. This module looks at the macro picture: are doctrines consistent?
Is precedent coverage adequate? Are there single points of failure?
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)


def _rid() -> str:
    return f"reform_{uuid.uuid4().hex[:12]}"


# -- Continuity Assessment ---------------------------------------------------


def assess_continuity(db: Any) -> dict:
    """Evaluate overall system continuity and institutional health.

    Returns:
        {
            "mission_drift_score": float (0=stable, 1=severe drift),
            "institutional_health": float (0-1),
            "dependency_risks": [...],
            "recommendations": [...]
        }
    """
    risks = []
    recs = []
    health_signals = []

    # 1. Doctrine consistency: check for domains without ratified doctrines
    try:
        doctrine_rows = db._conn.execute(
            """SELECT domain, status, COUNT(*) as cnt
               FROM doctrine_registry GROUP BY domain, status""",
        ).fetchall()

        domains_with_ratified = set()
        domains_all = set()
        for r in doctrine_rows:
            domains_all.add(r["domain"])
            if r["status"] == "ratified":
                domains_with_ratified.add(r["domain"])

        uncovered = domains_all - domains_with_ratified
        if uncovered:
            risks.append({
                "type": "doctrine_gap",
                "details": f"Domains without ratified doctrine: {sorted(uncovered)}",
            })
            recs.append(f"Ratify doctrines for domains: {sorted(uncovered)}")
        else:
            health_signals.append(1.0)

        # Ratio of ratified doctrines
        if domains_all:
            health_signals.append(len(domains_with_ratified) / len(domains_all))
    except Exception:
        health_signals.append(0.5)

    # 2. Precedent coverage
    try:
        prec_row = db._conn.execute(
            "SELECT COUNT(*) as cnt FROM precedent_records",
        ).fetchone()
        prec_count = prec_row["cnt"] if prec_row else 0

        if prec_count == 0:
            risks.append({
                "type": "no_precedents",
                "details": "No precedent records exist — decisions lack historical grounding",
            })
            recs.append("Create precedent records from governance reviews")
            health_signals.append(0.2)
        else:
            health_signals.append(min(1.0, prec_count / 10.0))
    except Exception:
        health_signals.append(0.5)

    # 3. Task completion trends
    try:
        task_rows = db._conn.execute(
            """SELECT status, COUNT(*) as cnt FROM tasks
               GROUP BY status""",
        ).fetchall()
        task_counts = {r["status"]: r["cnt"] for r in task_rows}
        completed = task_counts.get("completed", 0)
        failed = task_counts.get("failed", 0)
        total = completed + failed

        if total > 0:
            success_rate = completed / total
            health_signals.append(success_rate)
            if success_rate < 0.5:
                recs.append("Task success rate is below 50% — investigate root causes")
        else:
            health_signals.append(0.5)
    except Exception:
        health_signals.append(0.5)

    # 4. Cultural drift signals
    try:
        drift_row = db._conn.execute(
            """SELECT COUNT(*) as cnt FROM cultural_drift_events
               WHERE status = 'detected'""",
        ).fetchone()
        active_drifts = drift_row["cnt"] if drift_row else 0
        if active_drifts > 0:
            risks.append({
                "type": "active_drift",
                "details": f"{active_drifts} unresolved cultural drift events",
            })
            health_signals.append(max(0.0, 1.0 - active_drifts * 0.2))
        else:
            health_signals.append(1.0)
    except Exception:
        health_signals.append(0.5)

    # Compute overall scores
    institutional_health = (
        sum(health_signals) / len(health_signals)
        if health_signals
        else 0.5
    )

    # Mission drift: higher when more risks and lower health
    mission_drift = max(0.0, min(1.0, 1.0 - institutional_health))

    return {
        "mission_drift_score": round(mission_drift, 3),
        "institutional_health": round(institutional_health, 3),
        "dependency_risks": risks,
        "recommendations": recs,
    }


# -- Fragility Detection ----------------------------------------------------


def identify_fragilities(db: Any) -> list[dict]:
    """Find single points of failure in the system.

    Checks:
    - Task families with only one skill
    - Domains with no doctrine
    - Clusters with very low trust
    """
    fragilities = []

    # 1. Task families with only one active skill
    try:
        rows = db._conn.execute(
            """SELECT intent_family, COUNT(*) as cnt
               FROM skill_registry WHERE status = 'active'
               GROUP BY intent_family HAVING cnt = 1""",
        ).fetchall()
        for r in rows:
            fragilities.append({
                "type": "single_skill_family",
                "target": r["intent_family"],
                "details": "Only one active skill — no redundancy or fallback",
                "severity": "medium",
            })
    except Exception as e:
        logger.error("[CivPlanner] fragility check (skills) failed: %s", e)

    # 2. Domains with no ratified doctrine
    try:
        # Get all domains that appear in tasks or skills
        task_domains = set()
        try:
            rows = db._conn.execute(
                "SELECT DISTINCT task_type FROM tasks",
            ).fetchall()
            task_domains = {r["task_type"] for r in rows}
        except Exception:
            pass

        ratified_domains = set()
        try:
            rows = db._conn.execute(
                """SELECT DISTINCT domain FROM doctrine_registry
                   WHERE status = 'ratified'""",
            ).fetchall()
            ratified_domains = {r["domain"] for r in rows}
        except Exception:
            pass

        uncovered = task_domains - ratified_domains
        for domain in uncovered:
            fragilities.append({
                "type": "ungovened_domain",
                "target": domain,
                "details": f"Task domain '{domain}' has no ratified doctrine",
                "severity": "high",
            })
    except Exception as e:
        logger.error("[CivPlanner] fragility check (doctrines) failed: %s", e)

    # 3. Clusters with very low trust (< 0.2)
    try:
        rows = db._conn.execute(
            """SELECT id, cluster_name, trust_score
               FROM agent_clusters
               WHERE status = 'active' AND trust_score < 0.2""",
        ).fetchall()
        for r in rows:
            fragilities.append({
                "type": "low_trust_cluster",
                "target": r["id"],
                "details": f"Cluster '{r['cluster_name']}' has trust_score={r['trust_score']:.3f}",
                "severity": "high",
            })
    except Exception as e:
        logger.error("[CivPlanner] fragility check (clusters) failed: %s", e)

    return fragilities


# -- Reform Proposals --------------------------------------------------------


def propose_reform(
    db: Any,
    reform_type: str,
    target: str,
    proposal_definition: dict | str,
    risk_score: float = 0.3,
) -> str:
    """Propose an institutional reform.

    Stores the proposal as a governance_reviews record with review_type='reform'.

    Returns the reform review id.
    """
    rid = _rid()
    now = time.time()
    notes = (
        json.dumps(proposal_definition, ensure_ascii=False)
        if not isinstance(proposal_definition, str)
        else proposal_definition
    )

    def _do(conn):
        conn.execute(
            """INSERT INTO governance_reviews
               (id, review_type, subject_type, subject_id, risk_score,
                decision, notes, created_at, reviewer_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rid, "reform", reform_type, target,
                risk_score, "proposed", notes, now, "civilization_planner",
            ),
        )

    db._execute_write(_do)
    logger.info(
        "[CivPlanner] Reform proposed %s: type=%s target=%s risk=%.2f",
        rid, reform_type, target, risk_score,
    )
    return rid


# -- Health Snapshot ---------------------------------------------------------


def get_health_snapshot(db: Any) -> dict:
    """Comprehensive civilization health metrics.

    Returns:
        {
            "continuity": {...},
            "fragilities": [...],
            "doctrine_count": int,
            "precedent_count": int,
            "cluster_count": int,
            "active_deliberations": int,
            "pending_reforms": int,
        }
    """
    continuity = assess_continuity(db)
    fragilities = identify_fragilities(db)

    # Counts
    try:
        doctrine_row = db._conn.execute(
            "SELECT COUNT(*) as cnt FROM doctrine_registry WHERE status = 'ratified'",
        ).fetchone()
        doctrine_count = doctrine_row["cnt"] if doctrine_row else 0
    except Exception:
        doctrine_count = 0

    try:
        prec_row = db._conn.execute(
            "SELECT COUNT(*) as cnt FROM precedent_records",
        ).fetchone()
        precedent_count = prec_row["cnt"] if prec_row else 0
    except Exception:
        precedent_count = 0

    try:
        cluster_row = db._conn.execute(
            "SELECT COUNT(*) as cnt FROM agent_clusters WHERE status = 'active'",
        ).fetchone()
        cluster_count = cluster_row["cnt"] if cluster_row else 0
    except Exception:
        cluster_count = 0

    try:
        delib_row = db._conn.execute(
            "SELECT COUNT(*) as cnt FROM deliberation_sessions WHERE status = 'open'",
        ).fetchone()
        active_delib = delib_row["cnt"] if delib_row else 0
    except Exception:
        active_delib = 0

    try:
        reform_row = db._conn.execute(
            """SELECT COUNT(*) as cnt FROM governance_reviews
               WHERE review_type = 'reform' AND decision = 'proposed'""",
        ).fetchone()
        pending_reforms = reform_row["cnt"] if reform_row else 0
    except Exception:
        pending_reforms = 0

    return {
        "continuity": continuity,
        "fragilities": fragilities,
        "doctrine_count": doctrine_count,
        "precedent_count": precedent_count,
        "cluster_count": cluster_count,
        "active_deliberations": active_delib,
        "pending_reforms": pending_reforms,
    }
