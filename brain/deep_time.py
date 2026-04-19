"""Deep Time — memory spanning epochs and civilizational transitions.

Provides a persistent memory layer that outlives individual epochs,
recording significant events, collapses, reconstructions, treaties,
and other trans-civilizational knowledge.

Memory types:
  epoch, collapse, reconstruction, treaty,
  continuity_proof, existential_event, paradigm_shift
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

MEMORY_TYPES = (
    "epoch",
    "collapse",
    "reconstruction",
    "treaty",
    "continuity_proof",
    "existential_event",
    "paradigm_shift",
)


def _mid() -> str:
    return f"dtm_{uuid.uuid4().hex[:12]}"


# -- Write -------------------------------------------------------------------


def write_memory(
    db: Any,
    memory_type: str,
    content: dict | str,
    *,
    epoch_id: Optional[str] = None,
    lineage: Optional[dict | list] = None,
    confidence: float = 0.7,
) -> str:
    """Write a deep-time memory record.

    Returns the memory id.
    """
    memory_id = _mid()
    now = time.time()
    content_json = (
        json.dumps(content, ensure_ascii=False)
        if not isinstance(content, str) else content
    )
    lineage_json = (
        json.dumps(lineage, ensure_ascii=False)
        if lineage is not None else None
    )

    def _do(conn):
        conn.execute(
            """INSERT INTO deep_time_memory
               (id, memory_type, epoch_id, content_json, lineage_json,
                confidence, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (memory_id, memory_type, epoch_id, content_json, lineage_json,
             confidence, now),
        )

    db._execute_write(_do)
    logger.info(
        "[DeepTime] Wrote memory %s: type=%s epoch=%s confidence=%.2f",
        memory_id, memory_type, epoch_id, confidence,
    )
    return memory_id


# -- Retrieval ---------------------------------------------------------------


def retrieve(
    db: Any,
    memory_types: Optional[list[str]] = None,
    epoch_id: Optional[str] = None,
    query: Optional[str] = None,
    top_k: int = 10,
) -> list[dict]:
    """Retrieve deep-time memories with optional filtering.

    Supports keyword search in content_json when query is provided.
    """
    try:
        conditions: list[str] = []
        params: list = []

        if memory_types:
            placeholders = ",".join("?" for _ in memory_types)
            conditions.append(f"memory_type IN ({placeholders})")
            params.extend(memory_types)

        if epoch_id:
            conditions.append("epoch_id = ?")
            params.append(epoch_id)

        if query:
            conditions.append("content_json LIKE ?")
            params.append(f"%{query}%")

        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(top_k)

        rows = db._conn.execute(
            f"""SELECT * FROM deep_time_memory{where}
                ORDER BY created_at DESC LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[DeepTime] retrieve failed: %s", e)
        return []


def get_memory(db: Any, memory_id: str) -> Optional[dict]:
    """Get a single deep-time memory by ID."""
    try:
        row = db._conn.execute(
            "SELECT * FROM deep_time_memory WHERE id = ?", (memory_id,),
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("[DeepTime] get_memory failed: %s", e)
        return None


def get_epoch_memories(db: Any, epoch_id: str) -> list[dict]:
    """Get all deep-time memories for a given epoch."""
    try:
        rows = db._conn.execute(
            """SELECT * FROM deep_time_memory
               WHERE epoch_id = ?
               ORDER BY created_at DESC""",
            (epoch_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[DeepTime] get_epoch_memories failed: %s", e)
        return []


def get_stats(db: Any) -> dict:
    """Get memory counts grouped by memory_type."""
    try:
        rows = db._conn.execute(
            """SELECT memory_type, COUNT(*) as count
               FROM deep_time_memory
               GROUP BY memory_type
               ORDER BY count DESC""",
        ).fetchall()
        return {dict(r)["memory_type"]: dict(r)["count"] for r in rows}
    except Exception as e:
        logger.error("[DeepTime] get_stats failed: %s", e)
        return {}
