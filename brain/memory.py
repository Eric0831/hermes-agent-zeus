"""Layered Memory System — four-tier memory with freshness and confidence.

Layers:
  profile  — user preferences, language, style (long-lived)
  episodic — time-stamped task/event records (decays)
  semantic — stable rules, patterns, project knowledge (curated)
  skill    — verified reusable methods (promoted from episodic)

Each record has:
  - confidence (0-1): how trustworthy this memory is
  - freshness_score (0-1): decays over time, refreshed on access
  - supersedes_id: links to an older record this one replaces
"""

from __future__ import annotations

import json
import logging
import math
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

MEMORY_TYPES = ("profile", "episodic", "semantic", "skill")
FRESHNESS_HALF_LIFE_DAYS = 14  # freshness halves every 14 days


def _mid() -> str:
    return f"mem_{uuid.uuid4().hex[:12]}"


# ── Write ─────────────────────────────────────────────────────────


def write_memory(
    db: Any,
    memory_type: str,
    scope_id: str,
    content: dict[str, Any],
    *,
    scope_type: str = "session",
    title: Optional[str] = None,
    source_task_id: Optional[str] = None,
    confidence: float = 0.8,
    expires_at: Optional[float] = None,
    supersedes_id: Optional[str] = None,
) -> str:
    """Write a memory record. Returns memory_id."""
    if memory_type not in MEMORY_TYPES:
        raise ValueError(f"Invalid memory type: {memory_type} (valid: {MEMORY_TYPES})")

    mid = _mid()
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO memory_records
               (id, memory_type, scope_type, scope_id, title, content_json,
                source_task_id, confidence, freshness_score, is_active,
                created_at, updated_at, expires_at, supersedes_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (mid, memory_type, scope_type, scope_id, title,
             json.dumps(content, ensure_ascii=False, default=str),
             source_task_id, confidence, 1.0, 1,
             now, now, expires_at, supersedes_id),
        )
        # If superseding, deactivate the old record
        if supersedes_id:
            conn.execute(
                "UPDATE memory_records SET is_active = 0, updated_at = ? WHERE id = ?",
                (now, supersedes_id),
            )

    db._execute_write(_do)
    logger.debug("Memory %s written: type=%s scope=%s/%s", mid, memory_type, scope_type, scope_id)
    return mid


# ── Read / Search ─────────────────────────────────────────────────


def retrieve(
    db: Any,
    scope_id: str,
    *,
    memory_types: Optional[list[str]] = None,
    scope_type: str = "session",
    query: Optional[str] = None,
    top_k: int = 10,
    min_confidence: float = 0.3,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    """
    Retrieve relevant memories for a scope.

    Returns records sorted by relevance (freshness × confidence).
    """
    conditions = ["scope_id = ?"]
    params: list[Any] = [scope_id]

    if scope_type:
        conditions.append("scope_type = ?")
        params.append(scope_type)

    if memory_types:
        placeholders = ",".join("?" for _ in memory_types)
        conditions.append(f"memory_type IN ({placeholders})")
        params.extend(memory_types)

    if active_only:
        conditions.append("is_active = 1")

    conditions.append("confidence >= ?")
    params.append(min_confidence)

    where = " AND ".join(conditions)

    rows = db._conn.execute(
        f"""SELECT id, memory_type, scope_type, scope_id, title, content_json,
                   source_task_id, confidence, freshness_score, created_at,
                   updated_at, expires_at, supersedes_id
            FROM memory_records
            WHERE {where}
            ORDER BY (freshness_score * confidence) DESC
            LIMIT ?""",
        (*params, top_k),
    ).fetchall()

    now = time.time()
    results = []
    for r in rows:
        record = dict(r)
        # Recompute freshness based on age
        age_days = (now - record["created_at"]) / 86400
        record["freshness_score"] = _compute_freshness(age_days)
        # Skip expired
        if record.get("expires_at") and record["expires_at"] < now:
            continue
        # Keyword filter if query provided
        if query and not _matches_query(record, query):
            continue
        results.append(record)

    # Re-sort by recomputed freshness × confidence (DB ordering used stale values)
    results.sort(key=lambda r: r["freshness_score"] * r["confidence"], reverse=True)
    return results[:top_k]


def get_memory(db: Any, memory_id: str) -> Optional[dict[str, Any]]:
    """Get a single memory by ID."""
    row = db._conn.execute(
        "SELECT * FROM memory_records WHERE id = ?", (memory_id,)
    ).fetchone()
    return dict(row) if row else None


def get_memories_by_type(
    db: Any,
    memory_type: str,
    scope_id: str,
    *,
    active_only: bool = True,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Get all memories of a specific type for a scope."""
    conditions = ["memory_type = ?", "scope_id = ?"]
    params: list[Any] = [memory_type, scope_id]
    if active_only:
        conditions.append("is_active = 1")

    rows = db._conn.execute(
        f"""SELECT * FROM memory_records
            WHERE {' AND '.join(conditions)}
            ORDER BY created_at DESC LIMIT ?""",
        (*params, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Update ────────────────────────────────────────────────────────


def update_confidence(db: Any, memory_id: str, new_confidence: float) -> None:
    """Update the confidence score of a memory."""
    def _do(conn):
        conn.execute(
            "UPDATE memory_records SET confidence = ?, updated_at = ? WHERE id = ?",
            (max(0.0, min(1.0, new_confidence)), time.time(), memory_id),
        )
    db._execute_write(_do)


def deactivate(db: Any, memory_id: str, reason: str = "deactivated") -> None:
    """Deactivate a memory (soft delete)."""
    def _do(conn):
        conn.execute(
            "UPDATE memory_records SET is_active = 0, updated_at = ? WHERE id = ?",
            (time.time(), memory_id),
        )
    db._execute_write(_do)
    logger.debug("Memory %s deactivated: %s", memory_id, reason)


def refresh_freshness(db: Any, memory_id: str) -> None:
    """Refresh a memory's freshness (mark as recently relevant)."""
    def _do(conn):
        conn.execute(
            "UPDATE memory_records SET freshness_score = 1.0, updated_at = ? WHERE id = ?",
            (time.time(), memory_id),
        )
    db._execute_write(_do)


# ── Stats ─────────────────────────────────────────────────────────


def get_memory_stats(db: Any, scope_id: Optional[str] = None) -> dict[str, int]:
    """Get memory counts by type."""
    if scope_id:
        rows = db._conn.execute(
            """SELECT memory_type, COUNT(*) as cnt FROM memory_records
               WHERE scope_id = ? AND is_active = 1 GROUP BY memory_type""",
            (scope_id,),
        ).fetchall()
    else:
        rows = db._conn.execute(
            "SELECT memory_type, COUNT(*) as cnt FROM memory_records WHERE is_active = 1 GROUP BY memory_type"
        ).fetchall()

    stats = {t: 0 for t in MEMORY_TYPES}
    for r in rows:
        stats[r["memory_type"]] = r["cnt"]
    stats["total"] = sum(stats.values())
    return stats


# ── Helpers ───────────────────────────────────────────────────────


def _compute_freshness(age_days: float) -> float:
    """Exponential decay based on age."""
    return math.exp(-0.693 * age_days / FRESHNESS_HALF_LIFE_DAYS)  # ln(2) ≈ 0.693


def _matches_query(record: dict, query: str) -> bool:
    """Simple keyword matching for memory retrieval."""
    query_lower = query.lower()
    searchable = (
        (record.get("title") or "") + " " +
        str(record.get("content_json", ""))
    ).lower()
    query_words = query_lower.split()
    return any(w in searchable for w in query_words if len(w) > 2)
