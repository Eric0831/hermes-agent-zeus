"""Skill Engine — auto-generation, registry, ranking, and lifecycle.

Combines skill generation (from verified tasks) and registry (search,
rank, apply, deprecate) in a single module.

Skills are reusable execution patterns extracted from successful tasks:
  - Steps/tool chains that worked
  - Success criteria templates
  - Verification checklists
  - Fallback strategies

Only tasks that pass verification can generate skill candidates.
Candidates must be reviewed (auto or manual) before promotion.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _sid() -> str:
    return f"skill_{uuid.uuid4().hex[:12]}"


# ── Skill Generation ─────────────────────────────────────────────


def generate_candidate(
    db: Any,
    task: dict[str, Any],
    plan: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> Optional[str]:
    """
    Generate a skill candidate from a verified task.

    Only call this for tasks that passed verification.
    Returns skill_id if candidate created, None otherwise.
    """
    if task.get("status") != "completed":
        return None

    tools_used = sorted({
        e["tool_name"] for e in evidence if e.get("tool_name")
    })
    if not tools_used:
        return None

    # Build skill definition
    definition = {
        "intent": _infer_intent(task, plan),
        "task_type": task.get("task_type", "general"),
        "preconditions": [],
        "steps": _extract_steps(plan, evidence),
        "tools": tools_used,
        "success_criteria": plan.get("success_criteria", []),
        "verification_checklist": plan.get("success_criteria", []),
        "fallback_hints": plan.get("risks", []),
    }

    skill_name = _generate_name(task, plan)
    intent_family = _infer_intent(task, plan)
    sid = _sid()
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO skill_registry
               (id, skill_name, intent_family, version, status, definition_json,
                success_rate, usage_count, created_at, updated_at, risk_level,
                source_task_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sid, skill_name, intent_family, "1.0", "candidate",
             json.dumps(definition, ensure_ascii=False),
             1.0, 0, now, now,
             task.get("risk_level", "low"),
             task.get("id")),
        )

    db._execute_write(_do)
    logger.info("Skill candidate %s created: %s", sid, skill_name)
    return sid


def auto_promote(db: Any, skill_id: str) -> bool:
    """
    Auto-promote a candidate to active if it's low-risk.

    Returns True if promoted.
    """
    row = db._conn.execute(
        "SELECT status, risk_level FROM skill_registry WHERE id = ?",
        (skill_id,),
    ).fetchone()
    if not row or row["status"] != "candidate":
        return False

    if row["risk_level"] != "low":
        logger.debug("Skill %s is %s-risk, requires manual review",
                     skill_id, row["risk_level"])
        return False

    def _do(conn):
        conn.execute(
            "UPDATE skill_registry SET status = 'active', updated_at = ? WHERE id = ?",
            (time.time(), skill_id),
        )
    db._execute_write(_do)
    logger.info("Skill %s auto-promoted to active", skill_id)
    return True


# ── Skill Search & Retrieval ──────────────────────────────────────


def search_skills(
    db: Any,
    *,
    intent_family: Optional[str] = None,
    query: Optional[str] = None,
    status: str = "active",
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Search for applicable skills."""
    conditions = ["status = ?"]
    params: list[Any] = [status]

    if intent_family:
        conditions.append("intent_family = ?")
        params.append(intent_family)

    where = " AND ".join(conditions)

    rows = db._conn.execute(
        f"""SELECT id, skill_name, intent_family, version, status,
                   definition_json, success_rate, usage_count,
                   last_used_at, created_at, risk_level
            FROM skill_registry
            WHERE {where}
            ORDER BY (success_rate * 0.7 + (usage_count * 0.01)) DESC
            LIMIT ?""",
        (*params, top_k),
    ).fetchall()

    results = [dict(r) for r in rows]

    # Filter by query keywords if provided
    if query:
        query_lower = query.lower()
        results = [
            r for r in results
            if query_lower in r["skill_name"].lower()
            or query_lower in str(r.get("definition_json", "")).lower()
        ]

    return results


def get_skill(db: Any, skill_id: str) -> Optional[dict[str, Any]]:
    """Get a skill by ID."""
    row = db._conn.execute(
        "SELECT * FROM skill_registry WHERE id = ?", (skill_id,)
    ).fetchone()
    return dict(row) if row else None


# ── Skill Application ────────────────────────────────────────────


def record_application(
    db: Any,
    task_id: str,
    skill_id: str,
    status: str = "applied",
    result_summary: Optional[str] = None,
) -> str:
    """Record that a skill was applied to a task."""
    app_id = f"sa_{uuid.uuid4().hex[:12]}"
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO skill_applications
               (id, task_id, skill_id, status, result_summary, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (app_id, task_id, skill_id, status, result_summary, now),
        )
        # Increment usage count
        conn.execute(
            "UPDATE skill_registry SET usage_count = usage_count + 1, last_used_at = ?, updated_at = ? WHERE id = ?",
            (now, now, skill_id),
        )
    db._execute_write(_do)
    return app_id


def update_application(
    db: Any,
    app_id: str,
    status: str,
    result_summary: Optional[str] = None,
) -> None:
    """Update a skill application result."""
    def _do(conn):
        updates = ["status = ?"]
        params: list[Any] = [status]
        if result_summary:
            updates.append("result_summary = ?")
            params.append(result_summary)
        if status in ("succeeded", "failed"):
            updates.append("completed_at = ?")
            params.append(time.time())
        params.append(app_id)
        conn.execute(
            f"UPDATE skill_applications SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )
    db._execute_write(_do)


def update_success_rate(db: Any, skill_id: str) -> float:
    """Recompute success rate from application history."""
    rows = db._conn.execute(
        """SELECT status, COUNT(*) as cnt FROM skill_applications
           WHERE skill_id = ? AND status IN ('succeeded', 'failed')
           GROUP BY status""",
        (skill_id,),
    ).fetchall()

    counts = {r["status"]: r["cnt"] for r in rows}
    succeeded = counts.get("succeeded", 0)
    failed = counts.get("failed", 0)
    total = succeeded + failed

    if total == 0:
        return 0.0

    rate = succeeded / total

    def _do(conn):
        conn.execute(
            "UPDATE skill_registry SET success_rate = ?, updated_at = ? WHERE id = ?",
            (rate, time.time(), skill_id),
        )
    db._execute_write(_do)
    return rate


# ── Lifecycle ─────────────────────────────────────────────────────


def deprecate_skill(db: Any, skill_id: str, reason: str = "") -> None:
    """Mark a skill as deprecated."""
    def _do(conn):
        conn.execute(
            "UPDATE skill_registry SET status = 'deprecated', updated_at = ? WHERE id = ?",
            (time.time(), skill_id),
        )
    db._execute_write(_do)
    logger.info("Skill %s deprecated: %s", skill_id, reason)


def get_skill_stats(db: Any) -> dict[str, int]:
    """Get skill counts by status."""
    rows = db._conn.execute(
        "SELECT status, COUNT(*) as cnt FROM skill_registry GROUP BY status"
    ).fetchall()
    return {r["status"]: r["cnt"] for r in rows}


# ── Internal ──────────────────────────────────────────────────────


def _infer_intent(task: dict, plan: dict) -> str:
    """Infer the intent family for skill categorization."""
    goal = (plan.get("goal") or task.get("goal", "")).lower()
    task_type = task.get("task_type", "general")

    if task_type == "research":
        if "compare" in goal:
            return "research_compare"
        return "research_general"
    if task_type == "coding":
        if "fix" in goal or "bug" in goal:
            return "coding_fix"
        if "test" in goal:
            return "coding_test"
        return "coding_general"
    if task_type == "summary":
        return "summary"
    return "general"


def _extract_steps(plan: dict, evidence: list[dict]) -> list[dict]:
    """Extract execution steps from plan + evidence."""
    steps = []
    for s in plan.get("subtasks", []):
        step = {
            "description": s.get("description", ""),
            "tool": s.get("tool"),
        }
        # Check if we have evidence for this step's tool
        if s.get("tool"):
            has_evidence = any(
                e.get("tool_name") == s["tool"] for e in evidence
            )
            step["has_evidence"] = has_evidence
        steps.append(step)
    return steps


def _generate_name(task: dict, plan: dict) -> str:
    """Generate a descriptive skill name."""
    task_type = task.get("task_type", "general")
    goal = (plan.get("goal") or task.get("goal", ""))[:60]
    return f"{task_type}: {goal}"
