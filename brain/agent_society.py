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
