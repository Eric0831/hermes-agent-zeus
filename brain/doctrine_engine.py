"""Doctrine Engine — formal operating principles that guide system behavior.

Doctrines are named, versioned principles scoped to a domain (e.g. "governance",
"planning", "security"). They progress through a lifecycle:
  proposed -> under_review -> ratified -> archived
  proposed -> provisional -> ratified -> archived

Only ratified doctrines actively constrain the system. Archiving a doctrine
preserves it for precedent but removes it from active enforcement.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

STATUSES = ("proposed", "under_review", "ratified", "provisional", "archived")


def _did() -> str:
    return f"doc_{uuid.uuid4().hex[:12]}"


# -- Mutations ---------------------------------------------------------------


def propose_doctrine(
    db: Any,
    name: str,
    domain: str,
    definition: dict | str,
    *,
    ratified_by: Optional[str] = None,
) -> str:
    """Create a new doctrine in 'proposed' status.

    Returns the doctrine id.
    """
    did = _did()
    now = time.time()
    def_json = (
        json.dumps(definition, ensure_ascii=False)
        if not isinstance(definition, str)
        else definition
    )

    def _do(conn):
        conn.execute(
            """INSERT INTO doctrine_registry
               (id, doctrine_name, domain, version, status,
                definition_json, ratified_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (did, name, domain, 1, "proposed", def_json, ratified_by, now, now),
        )

    db._execute_write(_do)
    logger.info("[Doctrine] Proposed %s: %s (domain=%s)", did, name, domain)
    return did


def ratify_doctrine(
    db: Any,
    doctrine_id: str,
    ratified_by: str = "system",
) -> bool:
    """Ratify a doctrine — change status to 'ratified'.

    If a ratified doctrine with the same name+domain already exists, the new
    one gets an incremented version and the old one is archived.

    Returns True if ratified, False if doctrine not found or already ratified.
    """
    row = db._conn.execute(
        "SELECT * FROM doctrine_registry WHERE id = ?",
        (doctrine_id,),
    ).fetchone()
    if not row:
        logger.warning("[Doctrine] ratify: %s not found", doctrine_id)
        return False

    current = dict(row)
    if current["status"] == "ratified":
        return False

    # Check for existing ratified doctrine with same name+domain
    existing = db._conn.execute(
        """SELECT id, version FROM doctrine_registry
           WHERE doctrine_name = ? AND domain = ? AND status = 'ratified'
           ORDER BY version DESC LIMIT 1""",
        (current["doctrine_name"], current["domain"]),
    ).fetchone()

    new_version = (existing["version"] + 1) if existing else current["version"]
    now = time.time()

    def _do(conn):
        # Archive the old ratified one if it exists
        if existing:
            conn.execute(
                """UPDATE doctrine_registry
                   SET status = 'archived', updated_at = ?
                   WHERE id = ?""",
                (now, existing["id"]),
            )
        # Ratify the new one
        conn.execute(
            """UPDATE doctrine_registry
               SET status = 'ratified', version = ?,
                   ratified_by = ?, updated_at = ?
               WHERE id = ?""",
            (new_version, ratified_by, now, doctrine_id),
        )

    db._execute_write(_do)
    logger.info(
        "[Doctrine] Ratified %s v%d by %s", doctrine_id, new_version, ratified_by,
    )
    return True


def archive_doctrine(db: Any, doctrine_id: str) -> None:
    """Archive a doctrine — removes it from active enforcement."""
    now = time.time()

    def _do(conn):
        conn.execute(
            """UPDATE doctrine_registry
               SET status = 'archived', updated_at = ?
               WHERE id = ?""",
            (now, doctrine_id),
        )

    db._execute_write(_do)
    logger.info("[Doctrine] Archived %s", doctrine_id)


# -- Queries -----------------------------------------------------------------


def get_doctrine(db: Any, doctrine_id: str) -> Optional[dict]:
    """Get a single doctrine by ID."""
    try:
        row = db._conn.execute(
            "SELECT * FROM doctrine_registry WHERE id = ?",
            (doctrine_id,),
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("[Doctrine] get_doctrine failed: %s", e)
        return None


def get_active_doctrines(db: Any, domain: Optional[str] = None) -> list[dict]:
    """Get all ratified doctrines, optionally filtered by domain."""
    try:
        if domain:
            rows = db._conn.execute(
                """SELECT * FROM doctrine_registry
                   WHERE status = 'ratified' AND domain = ?
                   ORDER BY updated_at DESC""",
                (domain,),
            ).fetchall()
        else:
            rows = db._conn.execute(
                """SELECT * FROM doctrine_registry
                   WHERE status = 'ratified'
                   ORDER BY updated_at DESC""",
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[Doctrine] get_active_doctrines failed: %s", e)
        return []


def search_doctrines(
    db: Any,
    query: Optional[str] = None,
    domain: Optional[str] = None,
) -> list[dict]:
    """Search doctrines by keyword in name/definition and optional domain."""
    try:
        conditions: list[str] = []
        params: list[Any] = []

        if domain:
            conditions.append("domain = ?")
            params.append(domain)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        rows = db._conn.execute(
            f"""SELECT * FROM doctrine_registry
                {where}
                ORDER BY updated_at DESC""",
            tuple(params),
        ).fetchall()

        results = [dict(r) for r in rows]

        if query:
            q = query.lower()
            results = [
                r for r in results
                if q in r["doctrine_name"].lower()
                or q in (r.get("definition_json") or "").lower()
            ]

        return results
    except Exception as e:
        logger.error("[Doctrine] search_doctrines failed: %s", e)
        return []


def get_doctrine_stats(db: Any) -> dict:
    """Get counts of doctrines by status."""
    try:
        rows = db._conn.execute(
            """SELECT status, COUNT(*) as cnt
               FROM doctrine_registry GROUP BY status""",
        ).fetchall()
        counts = {s: 0 for s in STATUSES}
        for r in rows:
            counts[r["status"]] = r["cnt"]
        counts["total"] = sum(counts.values())
        return counts
    except Exception as e:
        logger.error("[Doctrine] get_doctrine_stats failed: %s", e)
        return {"total": 0}
