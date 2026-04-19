"""Precedent Store — past decisions that constrain future decisions.

Precedent records capture the outcome of governance reviews, doctrine
interpretations, conflict resolutions, and reform outcomes. Each record
has a binding_strength (0-1) indicating how firmly it should constrain
future similar decisions.

Precedent types:
  - governance_case: outcome of a governance review
  - doctrine_interpretation: how a doctrine was applied in a specific case
  - conflict_resolution: how a conflict between agents/clusters was resolved
  - reform_outcome: result of an institutional reform attempt
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

PRECEDENT_TYPES = (
    "governance_case",
    "doctrine_interpretation",
    "conflict_resolution",
    "reform_outcome",
)


def _pid() -> str:
    return f"prec_{uuid.uuid4().hex[:12]}"


# -- Mutations ---------------------------------------------------------------


def create_precedent(
    db: Any,
    precedent_type: str,
    subject_type: str,
    subject_id: str,
    decision: dict | str,
    *,
    binding_strength: float = 0.5,
    source_review_id: Optional[str] = None,
) -> str:
    """Create a new precedent record.

    Returns the precedent id.
    """
    pid = _pid()
    now = time.time()
    decision_json = (
        json.dumps(decision, ensure_ascii=False)
        if not isinstance(decision, str)
        else decision
    )

    def _do(conn):
        conn.execute(
            """INSERT INTO precedent_records
               (id, precedent_type, subject_type, subject_id,
                decision_json, binding_strength, source_review_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pid, precedent_type, subject_type, subject_id,
                decision_json, binding_strength, source_review_id, now,
            ),
        )

    db._execute_write(_do)
    logger.info(
        "[Precedent] Created %s: type=%s subject=%s:%s strength=%.2f",
        pid, precedent_type, subject_type, subject_id, binding_strength,
    )
    return pid


# -- Queries -----------------------------------------------------------------


def search_precedents(
    db: Any,
    subject_type: Optional[str] = None,
    precedent_type: Optional[str] = None,
    min_binding: float = 0.0,
    limit: int = 10,
) -> list[dict]:
    """Search precedent records with optional filters."""
    try:
        conditions = ["binding_strength >= ?"]
        params: list[Any] = [min_binding]

        if subject_type:
            conditions.append("subject_type = ?")
            params.append(subject_type)

        if precedent_type:
            conditions.append("precedent_type = ?")
            params.append(precedent_type)

        where = " AND ".join(conditions)

        rows = db._conn.execute(
            f"""SELECT * FROM precedent_records
                WHERE {where}
                ORDER BY binding_strength DESC, created_at DESC
                LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[Precedent] search_precedents failed: %s", e)
        return []


def get_precedent(db: Any, precedent_id: str) -> Optional[dict]:
    """Get a single precedent by ID."""
    try:
        row = db._conn.execute(
            "SELECT * FROM precedent_records WHERE id = ?",
            (precedent_id,),
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("[Precedent] get_precedent failed: %s", e)
        return None


def find_applicable_precedents(
    db: Any,
    subject_type: str,
    context_keywords: list[str],
) -> list[dict]:
    """Find precedents relevant to a decision context via keyword matching.

    Searches decision_json for any of the provided keywords and returns
    matching precedents ordered by binding_strength.
    """
    if not context_keywords:
        return []

    try:
        rows = db._conn.execute(
            """SELECT * FROM precedent_records
               WHERE subject_type = ?
               ORDER BY binding_strength DESC, created_at DESC""",
            (subject_type,),
        ).fetchall()

        results = []
        keywords_lower = [k.lower() for k in context_keywords]
        for row in rows:
            r = dict(row)
            decision_text = (r.get("decision_json") or "").lower()
            if any(kw in decision_text for kw in keywords_lower):
                results.append(r)

        return results
    except Exception as e:
        logger.error("[Precedent] find_applicable_precedents failed: %s", e)
        return []


def get_precedent_stats(db: Any) -> dict:
    """Get aggregate counts of precedent records."""
    try:
        rows = db._conn.execute(
            """SELECT precedent_type, COUNT(*) as cnt
               FROM precedent_records GROUP BY precedent_type""",
        ).fetchall()
        counts = {t: 0 for t in PRECEDENT_TYPES}
        for r in rows:
            counts[r["precedent_type"]] = r["cnt"]
        counts["total"] = sum(counts.values())

        # Average binding strength
        avg_row = db._conn.execute(
            "SELECT AVG(binding_strength) as avg_bs FROM precedent_records",
        ).fetchone()
        counts["avg_binding_strength"] = round(
            avg_row["avg_bs"], 3
        ) if avg_row and avg_row["avg_bs"] is not None else 0.0

        return counts
    except Exception as e:
        logger.error("[Precedent] get_precedent_stats failed: %s", e)
        return {"total": 0}
