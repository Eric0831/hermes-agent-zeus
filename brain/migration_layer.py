"""Migration Layer — plans and tracks civilization migrations between epochs.

Supports migration types:
  - governance_reboot: restart governance from scratch
  - doctrine_translation: translate doctrine to a new framework
  - ontology_shift: change the fundamental concepts the system operates on
  - post_collapse_rebuild: reconstruct after a systemic failure

Also records paradigm shifts — discontinuities in the system's world-model
that may or may not trigger a full migration.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

MIGRATION_TYPES = (
    "governance_reboot",
    "doctrine_translation",
    "ontology_shift",
    "post_collapse_rebuild",
)


def _mid() -> str:
    return f"migr_{uuid.uuid4().hex[:12]}"


def _psid() -> str:
    return f"pshf_{uuid.uuid4().hex[:12]}"


# -- Migration Lifecycle -----------------------------------------------------


def propose_migration(
    db: Any,
    source_epoch_id: str,
    migration_type: str,
    plan: dict | str,
) -> str:
    """Propose a new civilization migration.

    Returns the migration id.
    """
    migration_id = _mid()
    now = time.time()
    plan_json = (
        json.dumps(plan, ensure_ascii=False)
        if not isinstance(plan, str) else plan
    )

    def _do(conn):
        conn.execute(
            """INSERT INTO civilization_migrations
               (id, source_epoch_id, target_epoch_id, migration_type,
                plan_json, status, created_at, started_at, completed_at)
               VALUES (?, ?, NULL, ?, ?, 'proposed', ?, NULL, NULL)""",
            (migration_id, source_epoch_id, migration_type, plan_json, now),
        )

    db._execute_write(_do)
    logger.info(
        "[MigrationLayer] Proposed migration %s: type=%s source=%s",
        migration_id, migration_type, source_epoch_id,
    )
    return migration_id


def start_migration(
    db: Any,
    migration_id: str,
    target_epoch_id: str,
) -> None:
    """Start executing a proposed migration."""
    now = time.time()

    def _do(conn):
        conn.execute(
            """UPDATE civilization_migrations
               SET status = 'executing', target_epoch_id = ?, started_at = ?
               WHERE id = ?""",
            (target_epoch_id, now, migration_id),
        )

    db._execute_write(_do)
    logger.info(
        "[MigrationLayer] Started migration %s -> epoch %s",
        migration_id, target_epoch_id,
    )


def complete_migration(
    db: Any,
    migration_id: str,
    *,
    success: bool = True,
) -> None:
    """Complete a migration (success or failure)."""
    now = time.time()
    status = "completed" if success else "failed"

    def _do(conn):
        conn.execute(
            """UPDATE civilization_migrations
               SET status = ?, completed_at = ?
               WHERE id = ?""",
            (status, now, migration_id),
        )

    db._execute_write(_do)
    logger.info("[MigrationLayer] Migration %s -> %s", migration_id, status)


# -- Migration Queries -------------------------------------------------------


def get_migration(db: Any, migration_id: str) -> Optional[dict]:
    """Get a single migration by ID."""
    try:
        row = db._conn.execute(
            "SELECT * FROM civilization_migrations WHERE id = ?",
            (migration_id,),
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("[MigrationLayer] get_migration failed: %s", e)
        return None


def get_migrations(
    db: Any,
    status: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """Get migrations, optionally filtered by status."""
    try:
        if status:
            rows = db._conn.execute(
                """SELECT * FROM civilization_migrations
                   WHERE status = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (status, limit),
            ).fetchall()
        else:
            rows = db._conn.execute(
                """SELECT * FROM civilization_migrations
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[MigrationLayer] get_migrations failed: %s", e)
        return []


# -- Paradigm Shifts ---------------------------------------------------------


def record_paradigm_shift(
    db: Any,
    shift_type: str,
    description: dict | str,
    severity: str = "medium",
    epoch_id: Optional[str] = None,
) -> str:
    """Record a paradigm shift event.

    Returns the shift id.
    """
    shift_id = _psid()
    now = time.time()
    desc_json = (
        json.dumps(description, ensure_ascii=False)
        if not isinstance(description, str) else description
    )

    def _do(conn):
        conn.execute(
            """INSERT INTO paradigm_shifts
               (id, shift_type, description_json, severity, status, epoch_id,
                created_at, resolved_at)
               VALUES (?, ?, ?, ?, 'detected', ?, ?, NULL)""",
            (shift_id, shift_type, desc_json, severity, epoch_id, now),
        )

    db._execute_write(_do)
    logger.info(
        "[MigrationLayer] Paradigm shift %s: type=%s severity=%s",
        shift_id, shift_type, severity,
    )
    return shift_id


def get_paradigm_shifts(
    db: Any,
    epoch_id: Optional[str] = None,
) -> list[dict]:
    """Get paradigm shifts, optionally filtered by epoch."""
    try:
        if epoch_id:
            rows = db._conn.execute(
                """SELECT * FROM paradigm_shifts
                   WHERE epoch_id = ?
                   ORDER BY created_at DESC""",
                (epoch_id,),
            ).fetchall()
        else:
            rows = db._conn.execute(
                "SELECT * FROM paradigm_shifts ORDER BY created_at DESC",
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[MigrationLayer] get_paradigm_shifts failed: %s", e)
        return []
