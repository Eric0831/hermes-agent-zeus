"""Institutional Memory — civilization-level persistent memory.

Stores doctrine lineage, reform history, governance cases, risk lessons,
and other institutional knowledge. Unlike task-scoped memory, institutional
memory persists across sessions and informs long-horizon decisions.

Memory types:
  - doctrine: doctrine creation, ratification, and evolution records
  - precedent: precedent-setting decisions and their outcomes
  - governance_case: detailed governance review records
  - reform: institutional reform proposals and results
  - civilization_risk: identified risks and mitigation outcomes
  - lineage: chronological change history for a scope
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

MEMORY_TYPES = (
    "doctrine", "precedent", "governance_case",
    "reform", "civilization_risk", "lineage",
)


def _mid() -> str:
    return f"imem_{uuid.uuid4().hex[:12]}"


# -- Mutations ---------------------------------------------------------------


def write_memory(
    db: Any,
    memory_type: str,
    scope_type: str,
    scope_id: str,
    content: dict | str,
    *,
    lineage: Optional[dict | list] = None,
    confidence: float = 0.7,
) -> str:
    """Write an institutional memory record.

    Returns the memory id.
    """
    mid = _mid()
    now = time.time()
    content_json = (
        json.dumps(content, ensure_ascii=False)
        if not isinstance(content, str)
        else content
    )
    lineage_json = (
        json.dumps(lineage, ensure_ascii=False)
        if lineage is not None
        else None
    )

    def _do(conn):
        conn.execute(
            """INSERT INTO institutional_memory
               (id, memory_type, scope_type, scope_id,
                content_json, lineage_json, confidence, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                mid, memory_type, scope_type, scope_id,
                content_json, lineage_json, confidence, now, now,
            ),
        )

    db._execute_write(_do)
    logger.info(
        "[InstitutionalMem] Wrote %s: type=%s scope=%s:%s",
        mid, memory_type, scope_type, scope_id,
    )
    return mid


# -- Queries -----------------------------------------------------------------


def retrieve(
    db: Any,
    memory_types: Optional[list[str]] = None,
    query: Optional[str] = None,
    top_k: int = 10,
) -> list[dict]:
    """Retrieve institutional memories with optional type filter and keyword search."""
    try:
        conditions: list[str] = []
        params: list[Any] = []

        if memory_types:
            placeholders = ", ".join("?" for _ in memory_types)
            conditions.append(f"memory_type IN ({placeholders})")
            params.extend(memory_types)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        rows = db._conn.execute(
            f"""SELECT * FROM institutional_memory
                {where}
                ORDER BY updated_at DESC
                LIMIT ?""",
            (*params, top_k * 5 if query else top_k),
        ).fetchall()

        results = [dict(r) for r in rows]

        if query:
            q = query.lower()
            results = [
                r for r in results
                if q in (r.get("content_json") or "").lower()
                or q in (r.get("scope_id") or "").lower()
                or q in (r.get("scope_type") or "").lower()
            ]
            results = results[:top_k]

        return results
    except Exception as e:
        logger.error("[InstitutionalMem] retrieve failed: %s", e)
        return []


def get_memory(db: Any, memory_id: str) -> Optional[dict]:
    """Get a single institutional memory by ID."""
    try:
        row = db._conn.execute(
            "SELECT * FROM institutional_memory WHERE id = ?",
            (memory_id,),
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("[InstitutionalMem] get_memory failed: %s", e)
        return None


def get_lineage(db: Any, scope_type: str, scope_id: str) -> list[dict]:
    """Get chronological history of institutional memory for a scope.

    Returns all memory records for the given scope ordered by creation time,
    providing a complete lineage of changes.
    """
    try:
        rows = db._conn.execute(
            """SELECT * FROM institutional_memory
               WHERE scope_type = ? AND scope_id = ?
               ORDER BY created_at ASC""",
            (scope_type, scope_id),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[InstitutionalMem] get_lineage failed: %s", e)
        return []


def get_stats(db: Any) -> dict:
    """Get aggregate counts of institutional memory by type."""
    try:
        rows = db._conn.execute(
            """SELECT memory_type, COUNT(*) as cnt
               FROM institutional_memory GROUP BY memory_type""",
        ).fetchall()
        counts = {t: 0 for t in MEMORY_TYPES}
        for r in rows:
            counts[r["memory_type"]] = r["cnt"]
        counts["total"] = sum(counts.values())

        # Average confidence
        avg_row = db._conn.execute(
            "SELECT AVG(confidence) as avg_conf FROM institutional_memory",
        ).fetchone()
        counts["avg_confidence"] = round(
            avg_row["avg_conf"], 3
        ) if avg_row and avg_row["avg_conf"] is not None else 0.0

        return counts
    except Exception as e:
        logger.error("[InstitutionalMem] get_stats failed: %s", e)
        return {"total": 0}
