"""Evolution Core — unit CRUD and mutation management.

Manages the lifecycle of evolution units (skills, policies, verifier patterns,
planner patterns, doctrines, institution rules, migration schemas, treaty
patterns) across layers (micro through trans-civilizational).

Provides mutation tracking and inheritance lineage tracing for the full
evolution computation layer.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────

UNIT_TYPES = (
    "skill",
    "policy",
    "verifier_pattern",
    "planner_pattern",
    "doctrine",
    "institution_rule",
    "migration_schema",
    "treaty_pattern",
)

LAYERS = (
    "micro",
    "meso",
    "macro",
    "civilizational",
    "trans_civilizational",
)

STATUSES = (
    "candidate",
    "mutated",
    "evaluated",
    "governed",
    "trial",
    "adopted",
    "rejected",
    "retired",
    "archived",
)


# ── ID Generation ────────────────────────────────────────────────


def _unit_id() -> str:
    return f"eu_{uuid.uuid4().hex[:12]}"


def _mutation_id() -> str:
    return f"mut_{uuid.uuid4().hex[:12]}"


def _link_id() -> str:
    return f"inh_{uuid.uuid4().hex[:12]}"


# ── Unit CRUD ────────────────────────────────────────────────────


def create_unit(
    db: Any,
    unit_type: str,
    layer: str,
    family: str,
    definition: dict,
    *,
    version: str = "1.0",
    risk_level: str = "low",
    governance_scope: str = "auto",
    parent_unit_id: Optional[str] = None,
    inheritance_mode: Optional[str] = None,
    transmission_mode: Optional[str] = None,
    stability_class: Optional[str] = None,
) -> str:
    """Create an evolution unit with status='candidate'. Returns unit_id."""
    uid = _unit_id()
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO evolution_units
               (id, unit_type, layer, family, version, status, definition_json,
                parent_unit_id, risk_level, governance_scope,
                inheritance_mode, transmission_mode, stability_class,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                uid, unit_type, layer, family, version, "candidate",
                json.dumps(definition, ensure_ascii=False),
                parent_unit_id, risk_level, governance_scope,
                inheritance_mode, transmission_mode, stability_class,
                now, now,
            ),
        )

    db._execute_write(_do)
    logger.info(
        "Created evolution unit %s [%s/%s] family=%s",
        uid, unit_type, layer, family,
    )
    return uid


def mutate_unit(
    db: Any,
    unit_id: str,
    mutation_type: str,
    mutation_data: dict,
) -> str:
    """Record a mutation and update unit status to 'mutated'. Returns mutation_id."""
    mid = _mutation_id()
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO evolution_mutations
               (id, unit_id, mutation_type, mutation_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (mid, unit_id, mutation_type,
             json.dumps(mutation_data, ensure_ascii=False), now),
        )
        conn.execute(
            """UPDATE evolution_units SET status = 'mutated', updated_at = ?
               WHERE id = ?""",
            (now, unit_id),
        )

    db._execute_write(_do)
    logger.info("Mutated unit %s: type=%s -> mutation %s", unit_id, mutation_type, mid)
    return mid


def get_unit(db: Any, unit_id: str) -> Optional[dict]:
    """Fetch a single evolution unit by ID."""
    row = db._conn.execute(
        "SELECT * FROM evolution_units WHERE id = ?", (unit_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["definition"] = json.loads(d.pop("definition_json", "{}"))
    return d


def get_units(
    db: Any,
    *,
    family: Optional[str] = None,
    layer: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Query evolution units with optional filters."""
    conditions: list[str] = []
    params: list[Any] = []

    if family is not None:
        conditions.append("family = ?")
        params.append(family)
    if layer is not None:
        conditions.append("layer = ?")
        params.append(layer)
    if status is not None:
        conditions.append("status = ?")
        params.append(status)

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    rows = db._conn.execute(
        f"SELECT * FROM evolution_units{where} ORDER BY created_at DESC LIMIT ?",
        params,
    ).fetchall()

    results = []
    for row in rows:
        d = dict(row)
        d["definition"] = json.loads(d.pop("definition_json", "{}"))
        results.append(d)
    return results


def update_unit_status(db: Any, unit_id: str, new_status: str) -> None:
    """Update the status of an evolution unit."""
    now = time.time()

    def _do(conn):
        conn.execute(
            "UPDATE evolution_units SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, now, unit_id),
        )

    db._execute_write(_do)
    logger.info("Unit %s status -> %s", unit_id, new_status)


# ── Inheritance ──────────────────────────────────────────────────


def link_inheritance(
    db: Any,
    parent_unit_id: str,
    child_unit_id: str,
    inheritance_type: str,
) -> str:
    """Create an inheritance link between units. Returns link_id."""
    lid = _link_id()
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO inheritance_links
               (id, parent_unit_id, child_unit_id, inheritance_type, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (lid, parent_unit_id, child_unit_id, inheritance_type, now),
        )

    db._execute_write(_do)
    logger.info(
        "Linked inheritance %s: %s -> %s (%s)",
        lid, parent_unit_id, child_unit_id, inheritance_type,
    )
    return lid


def get_lineage(db: Any, unit_id: str) -> list[dict]:
    """Trace parent chain upward from a unit. Returns list from child to root."""
    lineage: list[dict] = []
    current_id = unit_id
    seen: set[str] = set()

    while current_id and current_id not in seen:
        seen.add(current_id)
        row = db._conn.execute(
            "SELECT * FROM inheritance_links WHERE child_unit_id = ?",
            (current_id,),
        ).fetchone()
        if not row:
            break
        d = dict(row)
        lineage.append(d)
        current_id = d["parent_unit_id"]

    return lineage


# ── Stats ────────────────────────────────────────────────────────


def get_unit_stats(db: Any) -> dict:
    """Aggregate counts by status, layer, and unit_type."""
    stats: dict[str, dict[str, int]] = {
        "by_status": {},
        "by_layer": {},
        "by_unit_type": {},
    }

    rows = db._conn.execute(
        "SELECT status, COUNT(*) as cnt FROM evolution_units GROUP BY status"
    ).fetchall()
    for r in rows:
        stats["by_status"][r["status"]] = r["cnt"]

    rows = db._conn.execute(
        "SELECT layer, COUNT(*) as cnt FROM evolution_units GROUP BY layer"
    ).fetchall()
    for r in rows:
        stats["by_layer"][r["layer"]] = r["cnt"]

    rows = db._conn.execute(
        "SELECT unit_type, COUNT(*) as cnt FROM evolution_units GROUP BY unit_type"
    ).fetchall()
    for r in rows:
        stats["by_unit_type"][r["unit_type"]] = r["cnt"]

    stats["total"] = sum(stats["by_status"].values())
    return stats
