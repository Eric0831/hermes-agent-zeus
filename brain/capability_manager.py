"""Capability Manager — version lifecycle for evolving capabilities.

Manages capability versions through a controlled lifecycle:
  proposed -> incubating -> experimental -> limited_rollout -> adopted

Each capability_family has at most one 'adopted' version at a time.
Adoption atomically deprecates the previous version (same pattern as
strategy.py's activate_strategy).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

CAPABILITY_STATUSES = (
    "proposed",
    "incubating",
    "experimental",
    "limited_rollout",
    "adopted",
    "deprecated",
    "retired",
)

# Valid forward transitions; any status can also go to deprecated/retired
# (except adopted needs governance approval for retirement).
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"incubating", "deprecated", "retired"},
    "incubating": {"experimental", "deprecated", "retired"},
    "experimental": {"limited_rollout", "deprecated", "retired"},
    "limited_rollout": {"adopted", "deprecated", "retired"},
    "adopted": {"deprecated"},  # retirement requires governance
    "deprecated": {"retired"},
    "retired": set(),
}


def _version_id() -> str:
    return f"capv_{uuid.uuid4().hex[:12]}"


# ── Public API ────────────────────────────────────────────────────


def create_version(
    db: Any,
    capability_family: str,
    definition: dict,
    *,
    source_proposal_id: Optional[str] = None,
    parent_version_id: Optional[str] = None,
) -> str:
    """Create a new capability version in 'proposed' status.

    Returns the version id.
    """
    vid = _version_id()
    now = time.time()
    version = _next_version(db, capability_family)

    def _do(conn):
        conn.execute(
            """INSERT INTO capability_versions
               (id, capability_family, version, status, definition_json,
                parent_version_id, source_proposal_id, created_at,
                activated_at, deprecated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                vid,
                capability_family,
                version,
                "proposed",
                json.dumps(definition, ensure_ascii=False, default=str),
                parent_version_id,
                source_proposal_id,
                now,
                None,
                None,
            ),
        )

    db._execute_write(_do)
    logger.info(
        "[CapabilityManager] Created %s v%d for family '%s'",
        vid, version, capability_family,
    )
    return vid


def transition_status(
    db: Any,
    version_id: str,
    new_status: str,
    reason: str = "",
) -> None:
    """Transition a capability version to a new status.

    Validates that the transition is allowed before applying.
    """
    if new_status not in CAPABILITY_STATUSES:
        raise ValueError(f"Invalid capability status: {new_status}")

    row = db._conn.execute(
        "SELECT * FROM capability_versions WHERE id = ?",
        (version_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Capability version not found: {version_id}")

    current = dict(row)
    current_status = current["status"]
    allowed = ALLOWED_TRANSITIONS.get(current_status, set())

    if new_status not in allowed:
        raise ValueError(
            f"Invalid transition: {current_status} -> {new_status} "
            f"(allowed: {allowed})"
        )

    now = time.time()

    def _do(conn):
        conn.execute(
            """UPDATE capability_versions
               SET status = ?, deprecated_at = ?
               WHERE id = ?""",
            (
                new_status,
                now if new_status in ("deprecated", "retired") else None,
                version_id,
            ),
        )

    db._execute_write(_do)
    logger.info(
        "[CapabilityManager] %s: %s -> %s (reason: %s)",
        version_id, current_status, new_status, reason or "none",
    )


def get_version(db: Any, version_id: str) -> Optional[dict]:
    """Get a capability version by ID."""
    row = db._conn.execute(
        "SELECT * FROM capability_versions WHERE id = ?",
        (version_id,),
    ).fetchone()
    return dict(row) if row else None


def promote_from_proposal(db: Any, proposal_id: str) -> str:
    """Bridge capability_proposals → capability_versions.

    Takes an 'approved' capability_proposal, creates a capability_version
    in 'incubating' status pre-linked via source_proposal_id, and moves
    the proposal's own status to 'incubating' so both sides stay in sync.

    The capability_family is derived as "{proposal_type}:{target_task_family}"
    so each (type, family) pair gets its own version sequence. Existing
    versions for the same family continue the sequence via _next_version.

    Raises ValueError when the proposal is missing, in the wrong status,
    or the transition fails.
    """
    from brain import evolution_architect

    p = evolution_architect.get_proposal(db, proposal_id)
    if not p:
        raise ValueError(f"Proposal not found: {proposal_id}")
    if p["status"] != "approved":
        raise ValueError(
            f"Proposal {proposal_id} is in status '{p['status']}' — "
            "only 'approved' can be promoted"
        )

    try:
        proposal_def = json.loads(p.get("proposal_json") or "{}")
    except Exception:
        proposal_def = {}

    capability_family = f"{p['proposal_type']}:{p['target_task_family']}"
    definition = {
        "proposal_type": p["proposal_type"],
        "target_task_family": p["target_task_family"],
        "title": p.get("title", ""),
        "expected_gain": p.get("expected_gain", 0.0),
        "risk_score": p.get("risk_score", 0.3),
        "source": proposal_def,
    }

    vid = create_version(
        db,
        capability_family=capability_family,
        definition=definition,
        source_proposal_id=proposal_id,
    )
    # Move the version forward from proposed to incubating (valid
    # transition per ALLOWED_TRANSITIONS).
    transition_status(db, vid, "incubating", reason="promoted_from_approved_proposal")

    # Keep the proposal in sync so it no longer appears in the 'approved'
    # backlog once it's actively being incubated as a version.
    evolution_architect.update_proposal_status(
        db, proposal_id, "incubating",
        reason=f"promoted_to_capability_version:{vid}",
    )
    logger.info(
        "[CapabilityManager] Promoted proposal %s -> version %s (family=%s)",
        proposal_id, vid, capability_family,
    )
    return vid


def get_active_version(db: Any, capability_family: str) -> Optional[dict]:
    """Get the currently adopted version for a capability family."""
    row = db._conn.execute(
        """SELECT * FROM capability_versions
           WHERE capability_family = ? AND status = 'adopted'
           LIMIT 1""",
        (capability_family,),
    ).fetchone()
    return dict(row) if row else None


def get_family_history(
    db: Any,
    capability_family: str,
    limit: int = 20,
) -> list[dict]:
    """Get version history for a capability family, newest first."""
    rows = db._conn.execute(
        """SELECT * FROM capability_versions
           WHERE capability_family = ?
           ORDER BY version DESC LIMIT ?""",
        (capability_family, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def adopt_version(db: Any, version_id: str) -> bool:
    """Atomically adopt a version: deprecate previous + activate new.

    The version must be in 'limited_rollout' status. Returns True on
    success, False if preconditions are not met.
    """
    try:
        row = db._conn.execute(
            "SELECT * FROM capability_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
        if not row:
            logger.warning("[CapabilityManager] Not found: %s", version_id)
            return False

        current = dict(row)
        if current["status"] != "limited_rollout":
            logger.warning(
                "[CapabilityManager] Cannot adopt %s — status is '%s' "
                "(must be 'limited_rollout')",
                version_id, current["status"],
            )
            return False

        now = time.time()
        family = current["capability_family"]

        def _do(conn):
            # Deprecate the current adopted version for this family
            conn.execute(
                """UPDATE capability_versions
                   SET status = 'deprecated', deprecated_at = ?
                   WHERE capability_family = ? AND status = 'adopted'""",
                (now, family),
            )
            # Adopt the new version
            conn.execute(
                """UPDATE capability_versions
                   SET status = 'adopted', activated_at = ?
                   WHERE id = ?""",
                (now, version_id),
            )

        db._execute_write(_do)
        logger.info(
            "[CapabilityManager] Adopted %s for family '%s' (v%s)",
            version_id, family, current["version"],
        )
        return True

    except Exception as e:
        logger.error("[CapabilityManager] adopt_version failed: %s", e)
        return False


def deprecate_version(db: Any, version_id: str) -> None:
    """Deprecate a capability version."""
    now = time.time()

    def _do(conn):
        conn.execute(
            """UPDATE capability_versions
               SET status = 'deprecated', deprecated_at = ?
               WHERE id = ?""",
            (now, version_id),
        )

    db._execute_write(_do)
    logger.info("[CapabilityManager] Deprecated %s", version_id)


def get_capability_stats(db: Any) -> dict:
    """Get aggregate counts of capability versions by status."""
    try:
        rows = db._conn.execute(
            """SELECT status, COUNT(*) as cnt
               FROM capability_versions
               GROUP BY status""",
        ).fetchall()

        counts = {s: 0 for s in CAPABILITY_STATUSES}
        for r in rows:
            counts[r["status"]] = r["cnt"]

        counts["total"] = sum(counts.values())
        return counts

    except Exception as e:
        logger.error("[CapabilityManager] get_capability_stats failed: %s", e)
        return {"total": 0}


# ── Internal ─────────────────────────────────────────────────────


def _next_version(db: Any, capability_family: str) -> int:
    """Determine the next version number for a capability family."""
    row = db._conn.execute(
        """SELECT MAX(version) as max_v FROM capability_versions
           WHERE capability_family = ?""",
        (capability_family,),
    ).fetchone()
    raw = row["max_v"] if row and row["max_v"] is not None else 0
    try:
        current = int(raw)
    except (TypeError, ValueError):
        current = 0
    return current + 1
