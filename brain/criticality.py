"""Criticality — edge-of-chaos detection for system stability.

Monitors cascade frequency, correlation length, and distance-to-critical
to determine whether the system is operating in a stable, elevated, or
critical regime.  Provides modifiers that throttle mutation and rollout
rates as the system approaches instability thresholds.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Thresholds ──────────────────────────────────────────────────

# distance_to_critical thresholds
_STABLE_THRESHOLD = 0.5       # > 0.5 → stable
_ELEVATED_THRESHOLD = 0.2     # 0.2–0.5 → elevated
                               # < 0.2 → critical

# ── ID Generation ───────────────────────────────────────────────


def _snapshot_id() -> str:
    return f"crt_{uuid.uuid4().hex[:12]}"


# ── Status Helpers ──────────────────────────────────────────────


def _classify_status(distance_to_critical: float) -> str:
    """Map distance_to_critical to a status label."""
    if distance_to_critical > _STABLE_THRESHOLD:
        return "stable"
    if distance_to_critical >= _ELEVATED_THRESHOLD:
        return "elevated"
    return "critical"


# ── Criticality Analysis ───────────────────────────────────────


def analyze_criticality(
    db: Any,
    scope_type: str,
    scope_id: str,
) -> dict:
    """Compute criticality indicators for a scope.

    Metrics:
      - cascade_frequency: rollback/rejection events per 24h window
      - correlation_length: avg number of related failures per incident
      - distance_to_critical: 1.0 - normalized risk accumulation

    Stores a snapshot and returns
    {snapshot_id, cascade_frequency, correlation_length,
     distance_to_critical, status}.
    """
    now = time.time()
    window = 86400  # 24 hours

    # Cascade frequency: count rollback / rejection events in window
    rollback_rows = db._conn.execute(
        """SELECT COUNT(*) AS cnt FROM selection_decisions
           WHERE decision = 'reject'
             AND created_at >= ?""",
        (now - window,),
    ).fetchone()
    rollback_count = dict(rollback_rows).get("cnt", 0) if rollback_rows else 0

    # Normalize: 0 rollbacks → 0.0, 10+ → 1.0
    cascade_frequency = round(min(rollback_count / 10.0, 1.0), 4)

    # Correlation length: count units with risk_level != 'low' as proxy
    # for correlated failures
    risk_rows = db._conn.execute(
        """SELECT COUNT(*) AS cnt FROM evolution_units
           WHERE risk_level IN ('high', 'critical')
             AND updated_at >= ?""",
        (now - window,),
    ).fetchone()
    risk_count = dict(risk_rows).get("cnt", 0) if risk_rows else 0

    total_rows = db._conn.execute(
        """SELECT COUNT(*) AS cnt FROM evolution_units
           WHERE updated_at >= ?""",
        (now - window,),
    ).fetchone()
    total_count = dict(total_rows).get("cnt", 0) if total_rows else 0

    if total_count > 0:
        correlation_length = round(risk_count / total_count, 4)
    else:
        correlation_length = 0.0

    # Distance to critical: 1.0 minus accumulated risk signals
    # Combined from cascade_frequency and correlation_length
    risk_accumulation = (cascade_frequency + correlation_length) / 2.0
    distance_to_critical = round(max(0.0, 1.0 - risk_accumulation), 4)

    status = _classify_status(distance_to_critical)

    # Persist snapshot
    sid = _snapshot_id()

    def _do(conn):
        conn.execute(
            """INSERT INTO criticality_snapshots
               (id, scope_type, scope_id, cascade_frequency,
                correlation_length, distance_to_critical, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sid, scope_type, scope_id, cascade_frequency,
                correlation_length, distance_to_critical, status, now,
            ),
        )

    db._execute_write(_do)
    logger.info(
        "Criticality %s: scope=%s/%s dist=%.4f status=%s",
        sid, scope_type, scope_id, distance_to_critical, status,
    )
    return {
        "snapshot_id": sid,
        "cascade_frequency": cascade_frequency,
        "correlation_length": correlation_length,
        "distance_to_critical": distance_to_critical,
        "status": status,
    }


# ── Status Queries ──────────────────────────────────────────────


def get_criticality_status(db: Any, scope_type: str, scope_id: str) -> str:
    """Return the latest criticality status for a scope.

    Returns 'stable' if no snapshot exists.
    """
    row = db._conn.execute(
        """SELECT status FROM criticality_snapshots
           WHERE scope_type = ? AND scope_id = ?
           ORDER BY created_at DESC LIMIT 1""",
        (scope_type, scope_id),
    ).fetchone()
    return dict(row)["status"] if row else "stable"


def get_snapshot(db: Any, snapshot_id: str) -> Optional[dict]:
    """Fetch a single criticality snapshot by ID."""
    row = db._conn.execute(
        "SELECT * FROM criticality_snapshots WHERE id = ?",
        (snapshot_id,),
    ).fetchone()
    return dict(row) if row else None


def get_latest_snapshot(db: Any, scope_type: str, scope_id: str) -> Optional[dict]:
    """Fetch the most recent snapshot for a scope."""
    row = db._conn.execute(
        """SELECT * FROM criticality_snapshots
           WHERE scope_type = ? AND scope_id = ?
           ORDER BY created_at DESC LIMIT 1""",
        (scope_type, scope_id),
    ).fetchone()
    return dict(row) if row else None


# ── Rate Modifiers ──────────────────────────────────────────────


def get_mutation_rate_modifier(criticality_status: str) -> float:
    """Return mutation rate multiplier based on criticality.

    stable   → 1.0  (normal mutation rate)
    elevated → 0.6  (reduced mutation)
    critical → 0.2  (minimal mutation)
    """
    return {"stable": 1.0, "elevated": 0.6, "critical": 0.2}.get(
        criticality_status, 1.0
    )


def get_rollout_modifier(criticality_status: str) -> float:
    """Return rollout modifier based on criticality.

    stable   → 1.0  (normal rollout)
    elevated → 0.5  (cautious rollout)
    critical → 0.0  (block all rollout)
    """
    return {"stable": 1.0, "elevated": 0.5, "critical": 0.0}.get(
        criticality_status, 1.0
    )
