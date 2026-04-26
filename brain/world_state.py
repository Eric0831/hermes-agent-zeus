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
import re
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


def get_world_state_summary(
    db: Any,
    session_id: str,
    *,
    goal: str = "",
    task_type: str = "",
) -> str:
    """Get a compact text summary of world state for prompt injection.

    When goal/task_type are provided, include a short learned-context
    section so the Planner can reuse active skills, precedents, and
    ratified doctrines without changing runtime behavior.
    """
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

    learned = _get_learned_context_summary(db, goal=goal, task_type=task_type)
    if learned:
        parts.append(learned)

    if not parts:
        return "(no notable world state)"

    return "\n".join(parts)


def _get_learned_context_summary(
    db: Any,
    *,
    goal: str = "",
    task_type: str = "",
) -> str:
    """Summarize reusable brain artifacts relevant to this task.

    This is prompt context only. It does not execute skills, ratify
    doctrines, or advance capability lifecycle state.
    """
    if db is None:
        return ""

    sections: list[str] = []
    try:
        skills = _get_relevant_skills(db, task_type=task_type, limit=3)
        if skills:
            sections.append("Reusable skills:")
            for s in skills:
                sections.append(f"  - {_format_skill(s)}")
    except Exception as e:
        logger.debug("Learned context skills unavailable: %s", e)

    try:
        precedents = _get_relevant_precedents(
            db, goal=goal, task_type=task_type, limit=3,
        )
        if precedents:
            sections.append("Relevant precedents:")
            for p in precedents:
                sections.append(f"  - {_format_precedent(p)}")
    except Exception as e:
        logger.debug("Learned context precedents unavailable: %s", e)

    try:
        doctrines = _get_ratified_doctrines(db, task_type=task_type, limit=3)
        if doctrines:
            sections.append("Ratified doctrines:")
            for d in doctrines:
                sections.append(f"  - {_format_doctrine(d)}")
    except Exception as e:
        logger.debug("Learned context doctrines unavailable: %s", e)

    if not sections:
        return ""
    return "Relevant learned context:\n" + "\n".join(sections)


def _get_relevant_skills(
    db: Any,
    *,
    task_type: str = "",
    limit: int = 3,
) -> list[dict[str, Any]]:
    tf = (task_type or "").strip().lower()
    if tf:
        if tf == "general":
            family_clause = "intent_family = ?"
            family_params: tuple[Any, ...] = (tf,)
        else:
            family_clause = "(intent_family = ? OR intent_family LIKE ?)"
            family_params = (tf, f"{tf}_%")
        rows = db._conn.execute(
            """SELECT id, skill_name, intent_family, definition_json,
                      success_rate, usage_count, risk_level
               FROM skill_registry
               WHERE status = 'active'
                     AND """ + family_clause + """
               ORDER BY (success_rate * 0.7 + (usage_count * 0.01)) DESC,
                        updated_at DESC
               LIMIT ?""",
            (*family_params, limit),
        ).fetchall()
        if rows:
            return [dict(r) for r in rows]

    rows = db._conn.execute(
        """SELECT id, skill_name, intent_family, definition_json,
                  success_rate, usage_count, risk_level
           FROM skill_registry
           WHERE status = 'active'
           ORDER BY (success_rate * 0.7 + (usage_count * 0.01)) DESC,
                    updated_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _get_relevant_precedents(
    db: Any,
    *,
    goal: str = "",
    task_type: str = "",
    limit: int = 3,
) -> list[dict[str, Any]]:
    from brain.precedent_hygiene import filter_clean_precedents

    tf = (task_type or "").strip().lower()
    rows = db._conn.execute(
        """SELECT id, precedent_type, subject_type, subject_id,
                  decision_json, binding_strength, created_at
           FROM precedent_records
           WHERE subject_type = 'task_family'
                 AND binding_strength >= 0.5
                 AND (? = '' OR subject_id = ? OR subject_id = 'general')
           ORDER BY binding_strength DESC, created_at DESC
           LIMIT ?""",
        (tf, tf, max(limit * 5, limit)),
    ).fetchall()
    results = filter_clean_precedents([dict(r) for r in rows], limit=limit)
    if results or not goal:
        return results

    # Fall back to keyword search over decision_json when the task family
    # has no direct precedents yet.
    keywords = _keywords(goal)[:5]
    if not keywords:
        return []
    clauses = " OR ".join(["lower(decision_json) LIKE ?"] * len(keywords))
    params = [f"%{k}%" for k in keywords]
    rows = db._conn.execute(
        f"""SELECT id, precedent_type, subject_type, subject_id,
                   decision_json, binding_strength, created_at
            FROM precedent_records
            WHERE binding_strength >= 0.5
                  AND ({clauses})
            ORDER BY binding_strength DESC, created_at DESC
            LIMIT ?""",
        (*params, max(limit * 5, limit)),
    ).fetchall()
    return filter_clean_precedents([dict(r) for r in rows], limit=limit)


def _get_ratified_doctrines(
    db: Any,
    *,
    task_type: str = "",
    limit: int = 3,
) -> list[dict[str, Any]]:
    tf = (task_type or "").strip().lower()
    rows = db._conn.execute(
        """SELECT id, doctrine_name, domain, definition_json, version
           FROM doctrine_registry
           WHERE status = 'ratified'
                 AND (? = '' OR domain = ? OR domain IN ('operations', 'quality'))
           ORDER BY updated_at DESC
           LIMIT ?""",
        (tf, tf, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _format_skill(row: dict[str, Any]) -> str:
    data = _loads(row.get("definition_json"))
    tools = data.get("tools") if isinstance(data, dict) else None
    tool_hint = ""
    if isinstance(tools, list) and tools:
        clean_tools = [
            str(t) for t in tools
            if str(t).strip() and str(t).strip().lower() != "unknown"
        ]
        if clean_tools:
            tool_hint = f"; tools={', '.join(clean_tools[:4])}"
    rate = float(row.get("success_rate") or 0.0)
    uses = int(row.get("usage_count") or 0)
    name = _compact(row.get("skill_name") or row.get("id") or "skill", 80)
    fam = row.get("intent_family") or "unknown"
    return f"{name} [{fam}] success={rate:.2f} uses={uses}{tool_hint}"


def _format_precedent(row: dict[str, Any]) -> str:
    data = _loads(row.get("decision_json"))
    if isinstance(data, dict):
        goal = data.get("goal") or data.get("decision") or row.get("precedent_type")
        verification = data.get("verification")
        evidence = data.get("evidence_count")
        details = _compact(str(goal or row.get("id")), 90)
        suffix = []
        if verification:
            suffix.append(f"verification={verification}")
        if evidence is not None:
            suffix.append(f"evidence={evidence}")
        if suffix:
            details = f"{details} ({', '.join(suffix)})"
    else:
        details = _compact(str(row.get("decision_json") or row.get("id")), 90)
    strength = float(row.get("binding_strength") or 0.0)
    subject = row.get("subject_id") or row.get("subject_type") or "unknown"
    return f"{subject}: {details}; binding={strength:.2f}"


def _format_doctrine(row: dict[str, Any]) -> str:
    data = _loads(row.get("definition_json"))
    if isinstance(data, dict):
        policy = data.get("policy") or data.get("rationale") or data.get("description")
    else:
        policy = row.get("definition_json")
    name = row.get("doctrine_name") or row.get("id") or "doctrine"
    domain = row.get("domain") or "global"
    return f"[{domain}] {_compact(str(name), 60)}: {_compact(str(policy or ''), 100)}"


def _loads(raw: Any) -> Any:
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


def _compact(text: str, limit: int) -> str:
    cleaned = " ".join(str(text).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)] + "..."


def _keywords(text: str) -> list[str]:
    words = re.findall(r"[\w\u4e00-\u9fff]{3,}", text.lower())
    stop = {"the", "and", "for", "with", "from", "this", "that"}
    return [w for w in words if w not in stop]


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
