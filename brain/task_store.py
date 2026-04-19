"""Task Store — CRUD and state machine for structured tasks.

Provides persistent task tracking with state transitions, success criteria,
and lifecycle management. All writes use the existing SessionDB._execute_write()
pattern (WAL mode + retry + jitter) for SQLite concurrency safety.

NOTE: SessionDB._execute_write() expects a callable fn(conn) — NOT raw SQL.
Reads can use db._conn directly (WAL allows concurrent readers).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── State Machine ─────────────────────────────────────────────────

TASK_STATES = (
    "received",
    "triaged",
    "planned",
    "running",
    "verifying",
    "completed",
    "failed",
    "blocked",
    "cancelled",
)

TERMINAL_STATES = {"completed", "failed", "cancelled"}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "received": {"triaged", "cancelled"},
    "triaged": {"planned", "running", "cancelled"},
    "planned": {"running", "cancelled"},
    "running": {"verifying", "failed", "blocked", "cancelled"},
    "verifying": {"completed", "failed", "blocked", "running"},
    "blocked": {"running", "cancelled"},
    "failed": {"running"},  # retry
    "completed": set(),
    "cancelled": set(),
}


def _validate_transition(from_state: str, to_state: str) -> None:
    allowed = ALLOWED_TRANSITIONS.get(from_state, set())
    if to_state not in allowed:
        raise ValueError(
            f"Invalid task transition: {from_state} -> {to_state} "
            f"(allowed: {allowed})"
        )


# ── ID Generation ─────────────────────────────────────────────────


def _task_id() -> str:
    return f"task_{uuid.uuid4().hex[:12]}"


# ── Task CRUD ─────────────────────────────────────────────────────


def create_task(
    db: Any,
    session_id: str,
    goal: str,
    *,
    event_text: str = "",
    task_type: str = "general",
    priority: str = "medium",
    risk_level: str = "low",
    parent_task_id: Optional[str] = None,
    requires_approval: bool = False,
    budget_tokens: Optional[int] = None,
    budget_ms: Optional[int] = None,
) -> str:
    """Create a new task. Returns task_id."""
    tid = _task_id()
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO tasks
               (id, parent_task_id, session_id, event_text, task_type,
                goal, status, priority, risk_level, requires_approval,
                budget_tokens, budget_ms, retry_count, max_retries,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tid, parent_task_id, session_id, event_text, task_type,
                goal, "received", priority, risk_level, int(requires_approval),
                budget_tokens, budget_ms, 0, 2,
                now, now,
            ),
        )
        # Log the initial transition within the same transaction
        conn.execute(
            """INSERT INTO task_transitions
               (task_id, from_state, to_state, reason, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (tid, "none", "received", "task_created", now),
        )

    db._execute_write(_do)
    logger.info("Created task %s [%s] goal=%s", tid, task_type, goal[:80])
    return tid


def update_task_status(
    db: Any,
    task_id: str,
    new_status: str,
    *,
    reason: str = "",
    plan_json: Optional[str] = None,
    failure_reason: Optional[str] = None,
    verification_status: Optional[str] = None,
    verification_json: Optional[str] = None,
) -> None:
    """Transition a task to a new status with validation.

    Both the status read and write happen inside a single BEGIN IMMEDIATE
    transaction to prevent TOCTOU races.
    """
    now = time.time()

    def _do(conn):
        # Read current status inside the write transaction to prevent races
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"Task not found: {task_id}")

        old_status = row["status"] if hasattr(row, "keys") else row[0]
        _validate_transition(old_status, new_status)

        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [new_status, now]

        if plan_json is not None:
            updates.append("plan_json = ?")
            params.append(plan_json)
        if failure_reason is not None:
            updates.append("failure_reason = ?")
            params.append(failure_reason)
        if verification_status is not None:
            updates.append("verification_status = ?")
            params.append(verification_status)
        if verification_json is not None:
            updates.append("verification_json = ?")
            params.append(verification_json)
        if new_status == "running" and old_status != "running":
            updates.append("started_at = ?")
            params.append(now)
        if new_status in TERMINAL_STATES:
            updates.append("completed_at = ?")
            params.append(now)

        params.append(task_id)
        conn.execute(
            f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )
        # Log transition in the same transaction
        conn.execute(
            """INSERT INTO task_transitions
               (task_id, from_state, to_state, reason, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (task_id, old_status, new_status, reason, now),
        )

    db._execute_write(_do)
    logger.debug("Task %s: -> %s (%s)", task_id, new_status, reason)


def get_task(db: Any, task_id: str) -> Optional[dict[str, Any]]:
    """Get a task by ID. Returns dict or None."""
    row = db._conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if not row:
        return None
    return dict(row)


def get_active_tasks(db: Any, session_id: str) -> list[dict[str, Any]]:
    """Get non-terminal tasks for a session."""
    rows = db._conn.execute(
        """SELECT id, task_type, goal, status, priority, risk_level,
                  created_at, started_at
           FROM tasks
           WHERE session_id = ? AND status NOT IN ('completed', 'failed', 'cancelled')
           ORDER BY created_at""",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_session_tasks(
    db: Any,
    session_id: str,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Get all tasks for a session (most recent first)."""
    rows = db._conn.execute(
        """SELECT id, task_type, goal, status, priority, risk_level,
                  verification_status, created_at, completed_at, failure_reason
           FROM tasks
           WHERE session_id = ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (session_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_task_with_details(db: Any, task_id: str) -> Optional[dict[str, Any]]:
    """Get a task with its criteria, transitions, and evidence summary."""
    task = get_task(db, task_id)
    if not task:
        return None

    task["criteria"] = get_criteria(db, task_id)
    task["transitions"] = get_transitions(db, task_id)

    row = db._conn.execute(
        "SELECT COUNT(*) as cnt FROM evidence_records WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    task["evidence_count"] = row["cnt"] if row else 0

    return task


# ── Criteria ──────────────────────────────────────────────────────


def save_criteria(db: Any, task_id: str, criteria: list[str]) -> None:
    """Save success criteria for a task."""
    now = time.time()

    def _do(conn):
        for i, desc in enumerate(criteria):
            conn.execute(
                """INSERT INTO task_criteria
                   (task_id, criterion_key, description, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (task_id, f"c{i}", desc, "pending", now, now),
            )

    db._execute_write(_do)


def update_criterion(
    db: Any,
    task_id: str,
    criterion_key: str,
    status: str,
    *,
    evidence_ids: Optional[list[str]] = None,
) -> None:
    """Update a criterion status."""

    def _do(conn):
        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, time.time()]
        if evidence_ids is not None:
            updates.append("evidence_ids = ?")
            params.append(json.dumps(evidence_ids))
        params.extend([task_id, criterion_key])
        conn.execute(
            f"UPDATE task_criteria SET {', '.join(updates)} "
            "WHERE task_id = ? AND criterion_key = ?",
            tuple(params),
        )

    db._execute_write(_do)


def get_criteria(db: Any, task_id: str) -> list[dict[str, Any]]:
    """Get all criteria for a task."""
    rows = db._conn.execute(
        """SELECT criterion_key, description, status, evidence_ids
           FROM task_criteria
           WHERE task_id = ?
           ORDER BY criterion_key""",
        (task_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Retry ─────────────────────────────────────────────────────────


def increment_retry(db: Any, task_id: str) -> tuple[int, int]:
    """Increment retry count. Returns (new_count, max_retries)."""
    row = db._conn.execute(
        "SELECT retry_count, max_retries FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Task not found: {task_id}")

    new_count = row["retry_count"] + 1

    def _do(conn):
        conn.execute(
            "UPDATE tasks SET retry_count = ?, updated_at = ? WHERE id = ?",
            (new_count, time.time(), task_id),
        )

    db._execute_write(_do)
    return new_count, row["max_retries"]


# ── Transitions ───────────────────────────────────────────────────


def get_transitions(db: Any, task_id: str) -> list[dict[str, Any]]:
    """Get state transition history for a task."""
    rows = db._conn.execute(
        """SELECT from_state, to_state, reason, created_at
           FROM task_transitions
           WHERE task_id = ?
           ORDER BY created_at""",
        (task_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Stats ─────────────────────────────────────────────────────────


def get_task_stats(db: Any, session_id: Optional[str] = None) -> dict[str, int]:
    """Get task counts by status."""
    if session_id:
        rows = db._conn.execute(
            """SELECT status, COUNT(*) as cnt FROM tasks
               WHERE session_id = ? GROUP BY status""",
            (session_id,),
        ).fetchall()
    else:
        rows = db._conn.execute(
            "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
        ).fetchall()

    stats: dict[str, int] = {s: 0 for s in TASK_STATES}
    for r in rows:
        stats[r["status"]] = r["cnt"]
    stats["total"] = sum(stats.values())
    return stats
