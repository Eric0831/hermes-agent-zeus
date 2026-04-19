"""Proactive Intelligence — detect problems and generate nudges.

Scans the system state for signals that require attention:
- Overdue tasks (running too long with no new evidence)
- Open loops (failed tasks not resumed or retried)
- Stale skills (registered but unused for extended periods)
- Risk accumulation (multiple high-risk tasks active simultaneously)

Generates proactive actions (reminders, nudges, alerts) that can be
surfaced to the user or auto-executed by the system.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

ACTION_TYPES = ("reminder", "nudge", "risk_alert", "stale_skill_warning")
RISK_LEVELS = ("low", "medium", "high", "critical")

# Thresholds (seconds)
OVERDUE_THRESHOLD = 5 * 60       # 5 minutes with no new evidence
OPEN_LOOP_THRESHOLD = 10 * 60   # Failed task not resumed within 10 min
STALE_SKILL_DAYS = 30            # Skill not used in 30 days
MAX_HIGH_RISK_CONCURRENT = 2     # More than this triggers an alert


def _action_id() -> str:
    return f"pact_{uuid.uuid4().hex[:12]}"


# -- Signal Evaluation -----------------------------------------------------


def evaluate_signals(db: Any, session_id: str) -> list[dict]:
    """Scan the current session for proactive signals.

    Returns a list of action dicts (not yet persisted). The caller
    can choose which ones to persist via ``create_action()``.
    """
    actions: list[dict] = []
    now = time.time()

    try:
        actions.extend(_check_overdue_tasks(db, session_id, now))
        actions.extend(_check_open_loops(db, session_id, now))
        actions.extend(_check_stale_skills(db, now))
        actions.extend(_check_risk_accumulation(db, session_id, now))
    except Exception as e:
        logger.error("[Proactive] Signal evaluation failed: %s", e)

    logger.debug(
        "[Proactive] Session %s: %d signals detected", session_id, len(actions),
    )
    return actions


# -- Action CRUD -----------------------------------------------------------


def create_action(
    db: Any,
    action_type: str,
    target_scope_type: str,
    target_scope_id: str,
    reason: str,
    *,
    risk_level: str = "low",
    requires_approval: bool = False,
) -> str:
    """Create a proactive action record.

    Returns the action id.
    """
    aid = _action_id()
    now = time.time()

    reason_json = json.dumps(
        {"reason": reason} if isinstance(reason, str) else reason,
        ensure_ascii=False,
        default=str,
    )

    def _do(conn):
        conn.execute(
            """INSERT INTO proactive_actions
               (id, action_type, target_scope_type, target_scope_id,
                reason_json, risk_level, requires_approval, status,
                created_at, executed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                aid, action_type, target_scope_type, target_scope_id,
                reason_json, risk_level, int(requires_approval),
                "pending", now, None,
            ),
        )

    db._execute_write(_do)
    logger.info(
        "[Proactive] Created action %s [%s] target=%s:%s",
        aid, action_type, target_scope_type, target_scope_id,
    )
    return aid


def get_pending_actions(
    db: Any,
    session_id: Optional[str] = None,
) -> list[dict]:
    """Get pending proactive actions, optionally filtered by session scope."""
    try:
        if session_id:
            rows = db._conn.execute(
                """SELECT * FROM proactive_actions
                   WHERE status = 'pending'
                     AND (target_scope_type = 'session'
                          AND target_scope_id = ?)
                   ORDER BY created_at""",
                (session_id,),
            ).fetchall()
        else:
            rows = db._conn.execute(
                """SELECT * FROM proactive_actions
                   WHERE status = 'pending'
                   ORDER BY created_at""",
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[Proactive] get_pending_actions failed: %s", e)
        return []


def execute_action(db: Any, action_id: str) -> None:
    """Mark a proactive action as executed."""
    now = time.time()

    def _do(conn):
        conn.execute(
            """UPDATE proactive_actions
               SET status = 'executed', executed_at = ?
               WHERE id = ?""",
            (now, action_id),
        )

    db._execute_write(_do)
    logger.debug("[Proactive] Executed action %s", action_id)


def format_nudge(action: dict) -> str:
    """Format a proactive action as user-friendly text."""
    action_type = action.get("action_type", "nudge")
    risk = action.get("risk_level", "low")

    # Parse the reason
    reason_raw = action.get("reason_json", "{}")
    try:
        reason_data = json.loads(reason_raw) if isinstance(reason_raw, str) else reason_raw
    except (json.JSONDecodeError, TypeError):
        reason_data = {"reason": str(reason_raw)}

    reason_text = reason_data.get("reason", "No details available")

    prefix_map = {
        "reminder": "[Reminder]",
        "nudge": "[Nudge]",
        "risk_alert": "[Risk Alert]",
        "stale_skill_warning": "[Stale Skill]",
    }
    prefix = prefix_map.get(action_type, "[Notice]")

    risk_indicator = ""
    if risk in ("high", "critical"):
        risk_indicator = f" (risk: {risk})"

    target = action.get("target_scope_id", "")
    target_suffix = f" [{target}]" if target else ""

    return f"{prefix} {reason_text}{risk_indicator}{target_suffix}"


# -- Internal Signal Checks ------------------------------------------------


def _check_overdue_tasks(
    db: Any, session_id: str, now: float,
) -> list[dict]:
    """Detect tasks running longer than OVERDUE_THRESHOLD with no recent evidence."""
    actions = []

    rows = db._conn.execute(
        """SELECT t.id, t.goal, t.started_at
           FROM tasks t
           WHERE t.session_id = ?
             AND t.status = 'running'
             AND t.started_at IS NOT NULL
             AND (? - t.started_at) > ?""",
        (session_id, now, OVERDUE_THRESHOLD),
    ).fetchall()

    for t in rows:
        task = dict(t)
        # Check if there is recent evidence
        latest = db._conn.execute(
            """SELECT MAX(created_at) as last_ev
               FROM evidence_records
               WHERE task_id = ?""",
            (task["id"],),
        ).fetchone()

        last_evidence_at = latest["last_ev"] if latest and latest["last_ev"] else 0
        if (now - last_evidence_at) > OVERDUE_THRESHOLD:
            elapsed = int(now - task["started_at"])
            actions.append({
                "action_type": "reminder",
                "target_scope_type": "task",
                "target_scope_id": task["id"],
                "reason": (
                    f"Task has been running for {elapsed}s with no recent evidence. "
                    f"Goal: {task['goal'][:80]}"
                ),
                "risk_level": "medium",
            })

    return actions


def _check_open_loops(
    db: Any, session_id: str, now: float,
) -> list[dict]:
    """Detect failed tasks that have not been retried or cancelled."""
    actions = []

    rows = db._conn.execute(
        """SELECT id, goal, completed_at, failure_reason
           FROM tasks
           WHERE session_id = ?
             AND status = 'failed'
             AND completed_at IS NOT NULL
             AND (? - completed_at) > ?""",
        (session_id, now, OPEN_LOOP_THRESHOLD),
    ).fetchall()

    for t in rows:
        task = dict(t)
        actions.append({
            "action_type": "nudge",
            "target_scope_type": "task",
            "target_scope_id": task["id"],
            "reason": (
                f"Failed task has not been retried or cancelled. "
                f"Goal: {task['goal'][:80]}. "
                f"Failure: {(task.get('failure_reason') or 'unknown')[:60]}"
            ),
            "risk_level": "low",
        })

    return actions


def _check_stale_skills(db: Any, now: float) -> list[dict]:
    """Detect registered skills that haven't been used recently."""
    actions = []
    cutoff = now - (STALE_SKILL_DAYS * 86400)

    try:
        rows = db._conn.execute(
            """SELECT id, skill_name, last_used_at
               FROM skill_registry
               WHERE status = 'active'
                 AND (last_used_at IS NULL OR last_used_at < ?)""",
            (cutoff,),
        ).fetchall()

        for s in rows:
            skill = dict(s)
            days_stale = (
                int((now - skill["last_used_at"]) / 86400)
                if skill.get("last_used_at")
                else STALE_SKILL_DAYS
            )
            actions.append({
                "action_type": "stale_skill_warning",
                "target_scope_type": "skill",
                "target_scope_id": skill["id"],
                "reason": (
                    f"Skill '{skill.get('skill_name', skill['id'])}' has not been used "
                    f"in {days_stale} days. Consider reviewing or deprecating."
                ),
                "risk_level": "low",
            })
    except Exception:
        # Skills table may not exist yet — non-fatal
        pass

    return actions


def _check_risk_accumulation(
    db: Any, session_id: str, now: float,
) -> list[dict]:
    """Detect when too many high-risk tasks are active simultaneously."""
    actions = []

    row = db._conn.execute(
        """SELECT COUNT(*) as cnt
           FROM tasks
           WHERE session_id = ?
             AND status IN ('running', 'planned', 'verifying')
             AND risk_level IN ('high', 'critical')""",
        (session_id,),
    ).fetchone()

    high_risk_count = row["cnt"] if row else 0

    if high_risk_count > MAX_HIGH_RISK_CONCURRENT:
        actions.append({
            "action_type": "risk_alert",
            "target_scope_type": "session",
            "target_scope_id": session_id,
            "reason": (
                f"{high_risk_count} high/critical-risk tasks are active concurrently. "
                f"Consider completing or pausing some before starting more."
            ),
            "risk_level": "high",
        })

    return actions
