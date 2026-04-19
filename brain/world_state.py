"""World Model Service — task-centric world state tracking.

Maintains a structured snapshot of "what's happening right now" for each
session, independent of the LLM's context window. The Planner and Executive
can query this to make better decisions.

World state is persisted in the existing `world_state` JSON column within
the sessions-level view (not a new table — uses the tasks + evidence tables
as the source of truth and computes a view on demand).

Phase 1: computed view from tasks/evidence. No separate world_state table needed yet.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


def get_world_state(db: Any, session_id: str) -> dict[str, Any]:
    """
    Compute the current world state for a session.

    Returns a structured snapshot:
    {
        "active_tasks": [...],
        "completed_tasks_count": int,
        "failed_tasks_count": int,
        "open_loops": [...],
        "recent_evidence": [...],
        "risk_flags": [...],
        "tools_used": [...],
        "session_health": "healthy" | "degraded" | "failing",
        "computed_at": float
    }
    """
    state: dict[str, Any] = {
        "active_tasks": [],
        "completed_tasks_count": 0,
        "failed_tasks_count": 0,
        "open_loops": [],
        "recent_evidence": [],
        "risk_flags": [],
        "tools_used": [],
        "session_health": "healthy",
        "computed_at": time.time(),
    }

    if db is None:
        return state

    try:
        _fill_task_state(db, session_id, state)
        _fill_evidence_state(db, session_id, state)
        _fill_risk_flags(state)
        _compute_health(state)
    except Exception as e:
        logger.debug("World state computation error (non-fatal): %s", e)

    return state


def get_world_state_summary(db: Any, session_id: str) -> str:
    """Get a compact text summary of world state for prompt injection."""
    ws = get_world_state(db, session_id)

    parts = []

    # Active tasks
    active = ws.get("active_tasks", [])
    if active:
        parts.append(f"Active tasks ({len(active)}):")
        for t in active[:5]:
            parts.append(f"  - [{t['status']}] {t['goal'][:80]}")

    # Open loops
    loops = ws.get("open_loops", [])
    if loops:
        parts.append(f"Open loops ({len(loops)}):")
        for l in loops[:3]:
            parts.append(f"  - {l['goal'][:80]} (blocked: {l.get('reason', '?')})")

    # Risk flags
    risks = ws.get("risk_flags", [])
    if risks:
        parts.append("Risk flags:")
        for r in risks[:3]:
            parts.append(f"  - {r}")

    # Health
    health = ws.get("session_health", "healthy")
    if health != "healthy":
        parts.append(f"Session health: {health}")

    if not parts:
        return "(no notable world state)"

    return "\n".join(parts)


# ── Internal Builders ─────────────────────────────────────────────


def _fill_task_state(db: Any, session_id: str, state: dict) -> None:
    """Fill task-related world state fields."""
    rows = db._conn.execute(
        """SELECT id, task_type, goal, status, priority, risk_level,
                  failure_reason, created_at, started_at, completed_at
           FROM tasks
           WHERE session_id = ?
           ORDER BY created_at DESC
           LIMIT 50""",
        (session_id,),
    ).fetchall()

    for r in rows:
        row = dict(r)
        status = row["status"]

        if status in ("received", "triaged", "planned", "running", "verifying"):
            state["active_tasks"].append({
                "id": row["id"],
                "task_type": row["task_type"],
                "goal": row["goal"],
                "status": status,
                "priority": row["priority"],
                "risk_level": row["risk_level"],
            })
        elif status == "completed":
            state["completed_tasks_count"] += 1
        elif status == "failed":
            state["failed_tasks_count"] += 1

        # Open loops: blocked or failed-retriable tasks
        if status in ("blocked", "failed"):
            state["open_loops"].append({
                "task_id": row["id"],
                "goal": row["goal"],
                "status": status,
                "reason": row.get("failure_reason", "unknown"),
            })


def _fill_evidence_state(db: Any, session_id: str, state: dict) -> None:
    """Fill recent evidence and tools-used from evidence records."""
    # Get recent evidence across all tasks in this session
    rows = db._conn.execute(
        """SELECT e.tool_name, e.source_type, e.summary, e.created_at
           FROM evidence_records e
           JOIN tasks t ON e.task_id = t.id
           WHERE t.session_id = ?
           ORDER BY e.created_at DESC
           LIMIT 10""",
        (session_id,),
    ).fetchall()

    tools_seen = set()
    for r in rows:
        row = dict(r)
        state["recent_evidence"].append({
            "tool_name": row["tool_name"],
            "source_type": row["source_type"],
            "summary": row["summary"][:100] if row["summary"] else "",
        })
        if row["tool_name"]:
            tools_seen.add(row["tool_name"])

    state["tools_used"] = sorted(tools_seen)


def _fill_risk_flags(state: dict) -> None:
    """Derive risk flags from task and evidence state."""
    # High-risk active tasks
    for t in state["active_tasks"]:
        if t["risk_level"] == "high":
            state["risk_flags"].append(
                f"High-risk task active: {t['goal'][:60]}"
            )

    # Too many open loops
    if len(state["open_loops"]) >= 3:
        state["risk_flags"].append(
            f"{len(state['open_loops'])} open loops — consider resolving before new tasks"
        )

    # High failure rate
    total = state["completed_tasks_count"] + state["failed_tasks_count"]
    if total >= 3 and state["failed_tasks_count"] / total > 0.5:
        state["risk_flags"].append(
            f"High failure rate: {state['failed_tasks_count']}/{total} tasks failed"
        )


def _compute_health(state: dict) -> None:
    """Compute overall session health."""
    if state["risk_flags"]:
        if len(state["risk_flags"]) >= 3:
            state["session_health"] = "failing"
        else:
            state["session_health"] = "degraded"
    else:
        state["session_health"] = "healthy"
