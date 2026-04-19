"""Agent Society — multi-agent cluster management, trust, and arbitration.

Manages agent clusters with roles, jurisdictions, trust scores, and
conflict resolution. Each cluster represents a logical group of capabilities
or responsibilities within the system.

Authority levels (ascending):
  - operational: day-to-day task execution
  - strategic: planning and resource allocation
  - governance: policy enforcement and review
  - constitutional: fundamental system rules and identity
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

AUTHORITY_LEVELS = ("operational", "strategic", "governance", "constitutional")


# -- Federation seed ---------------------------------------------------------
#
# ZEUS federal v38.2 topology: 5 market apostles + KIWI regime broadcast.
# Seeded into agent_clusters so Hermes (governance layer) can track
# jurisdictions, trust, and eventually route cross-layer tasks. Update
# here when the federation membership changes.
SEED_APOSTLES: list[tuple[str, dict, str]] = [
    ("OCEAN",  {"market": "TWSE",     "broker": "Sinopac", "region": "TW",     "asset": "equities"}, "operational"),
    ("ELEVEN", {"market": "TAIFEX",   "broker": "KGI",     "region": "TW",     "asset": "futures"},  "operational"),
    ("WILSON", {"market": "US",       "broker": "Futu",    "region": "US",     "asset": "equities"}, "operational"),
    ("SUSAN",  {"market": "overseas", "broker": "futures", "region": "global", "asset": "futures"},  "operational"),
    ("CRYPTO", {"market": "crypto",   "broker": "Binance", "region": "global", "asset": "crypto"},   "operational"),
    ("KIWI",   {"role": "regime_broadcast", "region": "global"},                                      "strategic"),
]


def _cid() -> str:
    return f"clust_{uuid.uuid4().hex[:12]}"


# -- Cluster CRUD -----------------------------------------------------------


def register_cluster(
    db: Any,
    name: str,
    jurisdiction: dict | list | str,
    *,
    authority_level: str = "operational",
) -> str:
    """Register a new agent cluster.

    Args:
        name: Human-readable cluster name
        jurisdiction: Dict/list describing what this cluster governs
        authority_level: One of operational, strategic, governance, constitutional

    Returns the cluster id.
    """
    cid = _cid()
    now = time.time()
    jurisdiction_json = (
        json.dumps(jurisdiction, ensure_ascii=False)
        if not isinstance(jurisdiction, str)
        else jurisdiction
    )

    def _do(conn):
        conn.execute(
            """INSERT INTO agent_clusters
               (id, cluster_name, jurisdiction_json, authority_level,
                trust_score, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (cid, name, jurisdiction_json, authority_level, 0.5, "active", now, now),
        )

    db._execute_write(_do)
    logger.info(
        "[Society] Registered cluster %s: %s (authority=%s)",
        cid, name, authority_level,
    )
    return cid


def get_cluster(db: Any, cluster_id: str) -> Optional[dict]:
    """Get a single cluster by ID."""
    try:
        row = db._conn.execute(
            "SELECT * FROM agent_clusters WHERE id = ?",
            (cluster_id,),
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("[Society] get_cluster failed: %s", e)
        return None


def get_all_clusters(db: Any, status: str = "active") -> list[dict]:
    """Get all clusters with given status."""
    try:
        rows = db._conn.execute(
            """SELECT * FROM agent_clusters
               WHERE status = ?
               ORDER BY authority_level DESC, trust_score DESC""",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[Society] get_all_clusters failed: %s", e)
        return []


def register_if_absent(
    db: Any,
    name: str,
    jurisdiction: dict | list | str,
    *,
    authority_level: str = "operational",
) -> tuple[str, bool]:
    """Register a cluster iff no active cluster with this name exists.

    Returns (cluster_id, newly_created). Idempotent so it can be called
    safely from gateway startup or a seeding script.
    """
    for c in get_all_clusters(db, status="active"):
        if c.get("cluster_name") == name:
            return c["id"], False
    cid = register_cluster(db, name=name, jurisdiction=jurisdiction, authority_level=authority_level)
    return cid, True


def seed_federation(db: Any, apostles: list[tuple[str, dict, str]] | None = None) -> dict[str, str]:
    """Ensure the canonical ZEUS federation clusters exist.

    Uses SEED_APOSTLES by default. Returns {name: cluster_id} and logs
    new vs existing entries. Safe to call on every gateway startup.
    """
    seed = apostles if apostles is not None else SEED_APOSTLES
    result: dict[str, str] = {}
    created = 0
    for name, jurisdiction, authority in seed:
        cid, new = register_if_absent(db, name=name, jurisdiction=jurisdiction, authority_level=authority)
        result[name] = cid
        if new:
            created += 1
    if created:
        logger.info("[Society] seed_federation: %d/%d new clusters registered", created, len(seed))
    else:
        logger.debug("[Society] seed_federation: all %d clusters already present", len(seed))
    return result


def deactivate_cluster(db: Any, cluster_id: str) -> None:
    """Deactivate a cluster."""
    now = time.time()

    def _do(conn):
        conn.execute(
            """UPDATE agent_clusters
               SET status = 'inactive', updated_at = ?
               WHERE id = ?""",
            (now, cluster_id),
        )

    db._execute_write(_do)
    logger.info("[Society] Deactivated cluster %s", cluster_id)


# -- Trust Management -------------------------------------------------------


def update_trust(db: Any, cluster_id: str, delta: float) -> float:
    """Adjust a cluster's trust_score by delta (clamped to 0-1).

    Returns the new trust score.
    """
    row = db._conn.execute(
        "SELECT trust_score FROM agent_clusters WHERE id = ?",
        (cluster_id,),
    ).fetchone()
    if not row:
        logger.warning("[Society] update_trust: cluster %s not found", cluster_id)
        return 0.0

    new_score = max(0.0, min(1.0, row["trust_score"] + delta))
    now = time.time()

    def _do(conn):
        conn.execute(
            """UPDATE agent_clusters
               SET trust_score = ?, updated_at = ?
               WHERE id = ?""",
            (new_score, now, cluster_id),
        )

    db._execute_write(_do)
    logger.info(
        "[Society] Trust updated for %s: %.3f -> %.3f (delta=%.3f)",
        cluster_id, row["trust_score"], new_score, delta,
    )
    return new_score


# -- Jurisdiction & Conflict -------------------------------------------------


def detect_jurisdiction_overlap(db: Any) -> list[dict]:
    """Find clusters with overlapping jurisdiction keywords.

    Parses jurisdiction_json for each active cluster and identifies pairs
    that share keywords.
    """
    try:
        rows = db._conn.execute(
            """SELECT id, cluster_name, jurisdiction_json
               FROM agent_clusters WHERE status = 'active'""",
        ).fetchall()

        clusters = []
        for r in rows:
            c = dict(r)
            try:
                j = json.loads(c["jurisdiction_json"])
                # Extract keywords from jurisdiction
                if isinstance(j, dict):
                    keywords = set()
                    for v in j.values():
                        if isinstance(v, str):
                            keywords.update(v.lower().split())
                        elif isinstance(v, list):
                            for item in v:
                                keywords.update(str(item).lower().split())
                    c["_keywords"] = keywords
                elif isinstance(j, list):
                    c["_keywords"] = {str(item).lower() for item in j}
                else:
                    c["_keywords"] = set(str(j).lower().split())
            except (json.JSONDecodeError, TypeError):
                c["_keywords"] = set()
            clusters.append(c)

        overlaps = []
        for i, a in enumerate(clusters):
            for b in clusters[i + 1:]:
                shared = a["_keywords"] & b["_keywords"]
                if shared:
                    overlaps.append({
                        "cluster_a_id": a["id"],
                        "cluster_a_name": a["cluster_name"],
                        "cluster_b_id": b["id"],
                        "cluster_b_name": b["cluster_name"],
                        "overlapping_keywords": sorted(shared),
                    })

        return overlaps
    except Exception as e:
        logger.error("[Society] detect_jurisdiction_overlap failed: %s", e)
        return []


def arbitrate_conflict(
    db: Any,
    cluster_a_id: str,
    cluster_b_id: str,
    conflict_type: str,
    resolution: dict | str,
) -> str:
    """Arbitrate a conflict between two clusters.

    Creates a precedent record from the resolution and returns the precedent id.
    """
    # Import here to avoid circular imports
    from brain.precedent_store import create_precedent

    resolution_data = resolution if isinstance(resolution, dict) else {"text": resolution}
    resolution_data["cluster_a_id"] = cluster_a_id
    resolution_data["cluster_b_id"] = cluster_b_id
    resolution_data["conflict_type"] = conflict_type

    pid = create_precedent(
        db,
        precedent_type="conflict_resolution",
        subject_type="cluster_conflict",
        subject_id=f"{cluster_a_id}:{cluster_b_id}",
        decision=resolution_data,
        binding_strength=0.7,
    )

    logger.info(
        "[Society] Arbitrated conflict between %s and %s -> precedent %s",
        cluster_a_id, cluster_b_id, pid,
    )
    return pid
