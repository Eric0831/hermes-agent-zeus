"""Deliberation — multi-agent deliberative senate for high-impact decisions.

Provides structured debate sessions where agent clusters submit positions
on proposals. Sessions are resolved by computing weighted consensus,
generating minority reports for any dissenting positions.

Position types:
  - support: in favor of the proposal
  - oppose: against the proposal
  - conditional_support: in favor with conditions
  - dissent: formal disagreement (recorded in minority report)
  - minority_report: detailed dissenting analysis
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

POSITION_TYPES = (
    "support", "oppose", "conditional_support", "dissent", "minority_report",
)

SUPPORT_TYPES = {"support", "conditional_support"}
OPPOSE_TYPES = {"oppose", "dissent"}


def _sid() -> str:
    return f"delib_{uuid.uuid4().hex[:12]}"


# -- Session Management ------------------------------------------------------


def open_session(
    db: Any,
    session_type: str,
    subject_type: str,
    subject_id: str,
) -> str:
    """Open a new deliberation session.

    Returns the session id.
    """
    sid = _sid()
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO deliberation_sessions
               (id, session_type, subject_type, subject_id,
                status, resolution_json, started_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (sid, session_type, subject_type, subject_id, "open", None, now, None),
        )

    db._execute_write(_do)
    logger.info(
        "[Deliberation] Opened session %s: type=%s subject=%s:%s",
        sid, session_type, subject_type, subject_id,
    )
    return sid


def submit_position(
    db: Any,
    session_id: str,
    cluster_id: str,
    position_type: str,
    position_data: dict | str,
    weight: float = 1.0,
) -> None:
    """Submit a position to a deliberation session."""
    now = time.time()
    position_json = (
        json.dumps(position_data, ensure_ascii=False)
        if not isinstance(position_data, str)
        else position_data
    )

    def _do(conn):
        conn.execute(
            """INSERT INTO deliberation_positions
               (session_id, cluster_id, position_type, position_json, weight, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, cluster_id, position_type, position_json, weight, now),
        )

    db._execute_write(_do)
    logger.info(
        "[Deliberation] Position submitted: session=%s cluster=%s type=%s",
        session_id, cluster_id, position_type,
    )


def get_positions(db: Any, session_id: str) -> list[dict]:
    """Get all positions for a deliberation session."""
    try:
        rows = db._conn.execute(
            """SELECT * FROM deliberation_positions
               WHERE session_id = ?
               ORDER BY created_at ASC""",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[Deliberation] get_positions failed: %s", e)
        return []


def resolve_session(db: Any, session_id: str) -> dict:
    """Resolve a deliberation session by computing weighted consensus.

    Counts support/oppose positions, computes consensus score,
    generates minority reports for dissenting positions.

    Returns:
        {
            "decision": "approved" | "rejected" | "deferred",
            "consensus_score": float (0-1),
            "support_weight": float,
            "oppose_weight": float,
            "minority_reports": [...]
        }
    """
    positions = get_positions(db, session_id)

    if not positions:
        resolution = {
            "decision": "deferred",
            "consensus_score": 0.0,
            "support_weight": 0.0,
            "oppose_weight": 0.0,
            "minority_reports": [],
            "reason": "no positions submitted",
        }
        _save_resolution(db, session_id, resolution)
        return resolution

    support_weight = 0.0
    oppose_weight = 0.0
    minority_reports = []

    for pos in positions:
        w = pos.get("weight", 1.0)
        pt = pos["position_type"]

        if pt in SUPPORT_TYPES:
            support_weight += w
        elif pt in OPPOSE_TYPES:
            oppose_weight += w

        # Collect minority reports and dissent positions
        if pt in ("dissent", "minority_report"):
            minority_reports.append({
                "cluster_id": pos["cluster_id"],
                "position_type": pt,
                "position": pos["position_json"],
            })

    total_weight = support_weight + oppose_weight
    if total_weight == 0:
        consensus_score = 0.0
    else:
        consensus_score = abs(support_weight - oppose_weight) / total_weight

    # Decision logic
    if support_weight > oppose_weight:
        decision = "approved"
    elif oppose_weight > support_weight:
        decision = "rejected"
    else:
        decision = "deferred"

    resolution = {
        "decision": decision,
        "consensus_score": round(consensus_score, 3),
        "support_weight": round(support_weight, 3),
        "oppose_weight": round(oppose_weight, 3),
        "minority_reports": minority_reports,
    }

    _save_resolution(db, session_id, resolution)
    logger.info(
        "[Deliberation] Resolved session %s: %s (consensus=%.3f)",
        session_id, decision, consensus_score,
    )
    return resolution


def _save_resolution(db: Any, session_id: str, resolution: dict) -> None:
    """Save resolution and mark session as resolved."""
    now = time.time()
    resolution_json = json.dumps(resolution, ensure_ascii=False)

    def _do(conn):
        conn.execute(
            """UPDATE deliberation_sessions
               SET status = 'resolved', resolution_json = ?, completed_at = ?
               WHERE id = ?""",
            (resolution_json, now, session_id),
        )

    db._execute_write(_do)


# -- Queries -----------------------------------------------------------------


def get_session(db: Any, session_id: str) -> Optional[dict]:
    """Get a single deliberation session by ID."""
    try:
        row = db._conn.execute(
            "SELECT * FROM deliberation_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("[Deliberation] get_session failed: %s", e)
        return None


def get_sessions(
    db: Any,
    status: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """Get deliberation sessions, optionally filtered by status."""
    try:
        if status:
            rows = db._conn.execute(
                """SELECT * FROM deliberation_sessions
                   WHERE status = ?
                   ORDER BY started_at DESC LIMIT ?""",
                (status, limit),
            ).fetchall()
        else:
            rows = db._conn.execute(
                """SELECT * FROM deliberation_sessions
                   ORDER BY started_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[Deliberation] get_sessions failed: %s", e)
        return []
