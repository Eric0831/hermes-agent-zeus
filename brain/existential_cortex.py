"""Existential Cortex — detects and manages existential-level risks.

Scans for risks that threaten the fundamental viability of the system:
  - mission_extinction: declining ratio of mission-related tasks
  - identity_fracture: continuity proofs with low scores
  - epistemic_collapse: very high task failure rate
  - governance_capture: single cluster with disproportionate deliberation weight
  - dependency_terminality: critical external dependency loss

Risk lifecycle: detected -> response_planned -> resolved
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

RISK_TYPES = (
    "mission_extinction",
    "identity_fracture",
    "epistemic_collapse",
    "governance_capture",
    "dependency_terminality",
)


def _eid() -> str:
    return f"exev_{uuid.uuid4().hex[:12]}"


# -- Risk Scanning -----------------------------------------------------------


def scan_risks(db: Any) -> list[dict]:
    """Scan for existential-level risk signals.

    Returns a list of detected risk signal dicts, each with
    {risk_type, severity, signals}.
    """
    detected: list[dict] = []

    try:
        _scan_mission_extinction(db, detected)
    except Exception as e:
        logger.warning("[ExistentialCortex] mission_extinction scan error: %s", e)

    try:
        _scan_identity_fracture(db, detected)
    except Exception as e:
        logger.warning("[ExistentialCortex] identity_fracture scan error: %s", e)

    try:
        _scan_epistemic_collapse(db, detected)
    except Exception as e:
        logger.warning("[ExistentialCortex] epistemic_collapse scan error: %s", e)

    try:
        _scan_governance_capture(db, detected)
    except Exception as e:
        logger.warning("[ExistentialCortex] governance_capture scan error: %s", e)

    logger.info("[ExistentialCortex] Scan complete: %d risks detected", len(detected))
    return detected


def _scan_mission_extinction(db: Any, detected: list[dict]) -> None:
    """Check for declining mission-related task ratio."""
    row = db._conn.execute(
        """SELECT COUNT(*) as total,
                  SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) as done
           FROM tasks
           WHERE created_at > ?""",
        (time.time() - 86400,),
    ).fetchone()
    if row:
        r = dict(row)
        total = r.get("total", 0)
        done = r.get("done", 0)
        if total > 10 and done / total < 0.1:
            detected.append({
                "risk_type": "mission_extinction",
                "severity": "high",
                "signals": [
                    f"Only {done}/{total} tasks completed in last 24h",
                ],
            })


def _scan_identity_fracture(db: Any, detected: list[dict]) -> None:
    """Check for continuity proofs with low scores."""
    rows = db._conn.execute(
        """SELECT continuity_score, verdict FROM continuity_proofs
           WHERE created_at > ?
           ORDER BY created_at DESC LIMIT 10""",
        (time.time() - 86400,),
    ).fetchall()
    low_scores = [dict(r) for r in rows if dict(r).get("continuity_score", 1.0) < 0.3]
    if len(low_scores) >= 2:
        detected.append({
            "risk_type": "identity_fracture",
            "severity": "critical",
            "signals": [
                f"{len(low_scores)} continuity proofs with score < 0.3 in last 24h",
            ],
        })


def _scan_epistemic_collapse(db: Any, detected: list[dict]) -> None:
    """Check for very high task failure rate."""
    row = db._conn.execute(
        """SELECT COUNT(*) as total,
                  SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
           FROM tasks
           WHERE created_at > ?""",
        (time.time() - 3600,),
    ).fetchone()
    if row:
        r = dict(row)
        total = r.get("total", 0)
        failed = r.get("failed", 0)
        if total > 5 and failed / total > 0.7:
            detected.append({
                "risk_type": "epistemic_collapse",
                "severity": "critical",
                "signals": [
                    f"{failed}/{total} tasks failed in last hour (>70%)",
                ],
            })


def _scan_governance_capture(db: Any, detected: list[dict]) -> None:
    """Check for single cluster with disproportionate deliberation weight."""
    rows = db._conn.execute(
        """SELECT cluster_id, SUM(weight) as total_weight
           FROM deliberation_positions
           WHERE created_at > ?
           GROUP BY cluster_id
           ORDER BY total_weight DESC""",
        (time.time() - 86400,),
    ).fetchall()
    if len(rows) >= 2:
        positions = [dict(r) for r in rows]
        total_all = sum(p.get("total_weight", 0) for p in positions)
        top_weight = positions[0].get("total_weight", 0)
        if total_all > 0 and top_weight / total_all > 0.8:
            detected.append({
                "risk_type": "governance_capture",
                "severity": "high",
                "signals": [
                    f"Cluster {positions[0].get('cluster_id')} holds "
                    f"{top_weight / total_all:.0%} of deliberation weight",
                ],
            })


# -- Risk Reporting ----------------------------------------------------------


def report_risk(
    db: Any,
    risk_type: str,
    severity: str,
    signals: list[str] | list[dict],
) -> str:
    """Report an existential risk event.

    Returns the event id.
    """
    event_id = _eid()
    now = time.time()
    signals_json = json.dumps(signals, ensure_ascii=False)

    def _do(conn):
        conn.execute(
            """INSERT INTO existential_events
               (id, risk_type, severity, signals_json, response_json,
                status, detected_at, resolved_at)
               VALUES (?, ?, ?, ?, NULL, 'detected', ?, NULL)""",
            (event_id, risk_type, severity, signals_json, now),
        )

    db._execute_write(_do)
    logger.info(
        "[ExistentialCortex] Reported risk %s: type=%s severity=%s",
        event_id, risk_type, severity,
    )
    return event_id


def get_risks(
    db: Any,
    status: str = "detected",
) -> list[dict]:
    """Get existential risk events by status."""
    try:
        rows = db._conn.execute(
            """SELECT * FROM existential_events
               WHERE status = ?
               ORDER BY detected_at DESC""",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[ExistentialCortex] get_risks failed: %s", e)
        return []


def plan_response(
    db: Any,
    event_id: str,
    response_plan: dict | str,
) -> None:
    """Attach a response plan to an existential event."""
    response_json = (
        json.dumps(response_plan, ensure_ascii=False)
        if not isinstance(response_plan, str) else response_plan
    )

    def _do(conn):
        conn.execute(
            """UPDATE existential_events
               SET response_json = ?, status = 'response_planned'
               WHERE id = ?""",
            (response_json, event_id),
        )

    db._execute_write(_do)
    logger.info("[ExistentialCortex] Response planned for %s", event_id)


def resolve_risk(db: Any, event_id: str) -> None:
    """Mark an existential event as resolved."""
    now = time.time()

    def _do(conn):
        conn.execute(
            """UPDATE existential_events
               SET status = 'resolved', resolved_at = ?
               WHERE id = ?""",
            (now, event_id),
        )

    db._execute_write(_do)
    logger.info("[ExistentialCortex] Resolved risk %s", event_id)
