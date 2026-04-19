"""Epoch Manager — manages civilization epochs (distinct eras of system existence).

Each epoch represents a coherent period of system operation with its own
identity, governance configuration, and operational context. When a new
epoch is created the previous active epoch is automatically closed.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _eid() -> str:
    return f"epoch_{uuid.uuid4().hex[:12]}"


# -- Epoch Lifecycle ---------------------------------------------------------


def create_epoch(
    db: Any,
    name: str,
    *,
    summary: Optional[dict | str] = None,
) -> str:
    """Create a new epoch, auto-closing the previous active one.

    Returns the new epoch id.
    """
    epoch_id = _eid()
    now = time.time()
    summary_json = (
        json.dumps(summary, ensure_ascii=False)
        if summary and not isinstance(summary, str)
        else summary
    )

    def _do(conn):
        # Close any currently active epoch
        conn.execute(
            """UPDATE epochs SET status = 'closed', ended_at = ?
               WHERE status = 'active'""",
            (now,),
        )
        conn.execute(
            """INSERT INTO epochs
               (id, epoch_name, status, summary_json, started_at, ended_at, created_at)
               VALUES (?, ?, 'active', ?, ?, NULL, ?)""",
            (epoch_id, name, summary_json, now, now),
        )

    db._execute_write(_do)
    logger.info("[EpochManager] Created epoch %s: %s", epoch_id, name)
    return epoch_id


def close_epoch(
    db: Any,
    epoch_id: str,
    summary: Optional[dict | str] = None,
) -> None:
    """Close an epoch by setting ended_at and status to 'closed'."""
    now = time.time()
    summary_json = (
        json.dumps(summary, ensure_ascii=False)
        if summary and not isinstance(summary, str)
        else summary
    )

    def _do(conn):
        if summary_json is not None:
            conn.execute(
                """UPDATE epochs
                   SET status = 'closed', ended_at = ?, summary_json = ?
                   WHERE id = ?""",
                (now, summary_json, epoch_id),
            )
        else:
            conn.execute(
                """UPDATE epochs SET status = 'closed', ended_at = ?
                   WHERE id = ?""",
                (now, epoch_id),
            )

    db._execute_write(_do)
    logger.info("[EpochManager] Closed epoch %s", epoch_id)


# -- Queries -----------------------------------------------------------------


def get_current_epoch(db: Any) -> Optional[dict]:
    """Get the currently active epoch, if any."""
    try:
        row = db._conn.execute(
            "SELECT * FROM epochs WHERE status = 'active' ORDER BY started_at DESC LIMIT 1",
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("[EpochManager] get_current_epoch failed: %s", e)
        return None


def get_epoch(db: Any, epoch_id: str) -> Optional[dict]:
    """Get a single epoch by ID."""
    try:
        row = db._conn.execute(
            "SELECT * FROM epochs WHERE id = ?", (epoch_id,),
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("[EpochManager] get_epoch failed: %s", e)
        return None


def get_epoch_history(db: Any, limit: int = 20) -> list[dict]:
    """Get epoch history, most recent first."""
    try:
        rows = db._conn.execute(
            "SELECT * FROM epochs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[EpochManager] get_epoch_history failed: %s", e)
        return []
