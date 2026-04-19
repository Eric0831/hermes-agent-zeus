"""Strategy Versioning — propose, activate, and rollback strategy versions.

Manages versioned strategy definitions for the brain's configurable policies:
- planner_policy: how plans are generated (tool selection, depth, etc.)
- verifier_policy: how verification strictness is tuned
- router_policy: how tasks are routed and prioritized

Each strategy goes through a lifecycle: proposed -> active -> deprecated.
Only one strategy per (strategy_type, scope_id) can be active at a time.
Rollback reverts to the previous active version.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

STRATEGY_TYPES = ("planner_policy", "verifier_policy", "router_policy")
STRATEGY_STATUSES = ("proposed", "active", "deprecated", "rolled_back")


def _strategy_id() -> str:
    return f"stv_{uuid.uuid4().hex[:12]}"


# -- Proposal & Lifecycle --------------------------------------------------


def propose_strategy(
    db: Any,
    strategy_type: str,
    scope_id: str,
    definition: dict,
    *,
    source_run_id: Optional[str] = None,
    confidence: float = 0.5,
) -> str:
    """Create a new strategy proposal.

    The proposal starts in 'proposed' status. It must be explicitly
    activated via ``activate_strategy()`` to take effect.

    Returns the new strategy version id.
    """
    sid = _strategy_id()
    now = time.time()
    version = _next_version(db, strategy_type, scope_id)

    def _do(conn):
        conn.execute(
            """INSERT INTO strategy_versions
               (id, strategy_type, scope_id, version, status,
                definition_json, source_run_id, created_at,
                activated_at, deprecated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sid, strategy_type, scope_id, version, "proposed",
                json.dumps(definition, ensure_ascii=False, default=str),
                source_run_id, now, None, None,
            ),
        )

    db._execute_write(_do)
    logger.info(
        "[Strategy] Proposed %s v%d for scope '%s' (confidence=%.2f)",
        strategy_type, version, scope_id, confidence,
    )
    return sid


def activate_strategy(db: Any, strategy_id: str) -> bool:
    """Activate a proposed strategy, deprecating the current active one.

    Returns True if activation succeeded, False if the strategy was not
    found or not in 'proposed' status.
    """
    try:
        row = db._conn.execute(
            "SELECT * FROM strategy_versions WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        if not row:
            logger.warning("[Strategy] Not found: %s", strategy_id)
            return False

        row = dict(row)
        if row["status"] != "proposed":
            logger.warning(
                "[Strategy] Cannot activate %s — status is '%s'",
                strategy_id, row["status"],
            )
            return False

        now = time.time()

        def _do(conn):
            # Deprecate the current active strategy for this type+scope
            conn.execute(
                """UPDATE strategy_versions
                   SET status = 'deprecated', deprecated_at = ?
                   WHERE strategy_type = ? AND scope_id = ?
                     AND status = 'active'""",
                (now, row["strategy_type"], row["scope_id"]),
            )
            # Activate the new one
            conn.execute(
                """UPDATE strategy_versions
                   SET status = 'active', activated_at = ?
                   WHERE id = ?""",
                (now, strategy_id),
            )

        db._execute_write(_do)
        logger.info(
            "[Strategy] Activated %s (type=%s scope=%s v%s)",
            strategy_id, row["strategy_type"], row["scope_id"], row["version"],
        )
        return True

    except Exception as e:
        logger.error("[Strategy] activate_strategy failed: %s", e)
        return False


def rollback_strategy(db: Any, strategy_id: str) -> bool:
    """Rollback an active strategy to the previous version.

    Marks the current strategy as 'rolled_back' and re-activates the
    most recent deprecated version for the same type+scope.

    Returns True if rollback succeeded.
    """
    try:
        row = db._conn.execute(
            "SELECT * FROM strategy_versions WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        if not row:
            logger.warning("[Strategy] Not found for rollback: %s", strategy_id)
            return False

        row = dict(row)
        if row["status"] != "active":
            logger.warning(
                "[Strategy] Cannot rollback %s — status is '%s'",
                strategy_id, row["status"],
            )
            return False

        # Find the previous version to restore
        prev = db._conn.execute(
            """SELECT id FROM strategy_versions
               WHERE strategy_type = ? AND scope_id = ?
                 AND status = 'deprecated'
               ORDER BY version DESC
               LIMIT 1""",
            (row["strategy_type"], row["scope_id"]),
        ).fetchone()

        now = time.time()

        def _do(conn):
            # Mark current as rolled back
            conn.execute(
                """UPDATE strategy_versions
                   SET status = 'rolled_back', deprecated_at = ?
                   WHERE id = ?""",
                (now, strategy_id),
            )
            # Re-activate previous if one exists
            if prev:
                conn.execute(
                    """UPDATE strategy_versions
                       SET status = 'active', activated_at = ?, deprecated_at = NULL
                       WHERE id = ?""",
                    (now, prev["id"]),
                )

        db._execute_write(_do)

        if prev:
            logger.info(
                "[Strategy] Rolled back %s, restored %s",
                strategy_id, prev["id"],
            )
        else:
            logger.info(
                "[Strategy] Rolled back %s (no previous version to restore)",
                strategy_id,
            )
        return True

    except Exception as e:
        logger.error("[Strategy] rollback_strategy failed: %s", e)
        return False


# -- Queries ---------------------------------------------------------------


def get_active_strategy(
    db: Any,
    strategy_type: str,
    scope_id: str,
) -> Optional[dict]:
    """Get the currently active strategy for a type+scope pair."""
    try:
        row = db._conn.execute(
            """SELECT * FROM strategy_versions
               WHERE strategy_type = ? AND scope_id = ? AND status = 'active'
               LIMIT 1""",
            (strategy_type, scope_id),
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("[Strategy] get_active_strategy failed: %s", e)
        return None


def get_strategy_history(
    db: Any,
    scope_id: str,
    limit: int = 10,
) -> list[dict]:
    """Get the version history for a scope, most recent first."""
    try:
        rows = db._conn.execute(
            """SELECT * FROM strategy_versions
               WHERE scope_id = ?
               ORDER BY version DESC
               LIMIT ?""",
            (scope_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[Strategy] get_strategy_history failed: %s", e)
        return []


# -- Internal --------------------------------------------------------------


def _next_version(db: Any, strategy_type: str, scope_id: str) -> int:
    """Determine the next version number for a type+scope pair."""
    row = db._conn.execute(
        """SELECT MAX(CAST(version AS INTEGER)) as max_v FROM strategy_versions
           WHERE strategy_type = ? AND scope_id = ?""",
        (strategy_type, scope_id),
    ).fetchone()
    raw = row["max_v"] if row and row["max_v"] is not None else "0"
    try:
        current = int(raw)
    except (TypeError, ValueError):
        current = 0
    return current + 1
