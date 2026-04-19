"""Memory Curator — decides what to remember, promote, or forget.

After each task completion, the curator:
1. Ingests the task postmortem
2. Decides what memories to create (episodic, semantic, profile)
3. Identifies skill promotion candidates
4. Detects stale memories to demote
5. Resolves conflicts between memories

No LLM needed for Phase 1 — uses rule-based curation.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from brain import memory as mem

logger = logging.getLogger(__name__)


# ── Post-Task Curation ────────────────────────────────────────────


def curate_after_task(
    db: Any,
    task_id: str,
    task: dict[str, Any],
    evidence: list[dict[str, Any]],
    verification_status: str,
    plan: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Run memory curation after a task completes.

    Returns a summary of actions taken:
    {
        "episodic_written": str | None,  # memory_id
        "semantic_written": str | None,
        "profile_written": str | None,
        "skill_candidate": bool,
        "stale_demoted": int,
    }
    """
    result = {
        "episodic_written": None,
        "semantic_written": None,
        "profile_written": None,
        "skill_candidate": False,
        "stale_demoted": 0,
    }

    session_id = task.get("session_id", "unknown")

    # 1. Always write episodic memory for the task
    episodic_content = _build_episodic_content(task, evidence, verification_status)
    result["episodic_written"] = mem.write_memory(
        db,
        memory_type="episodic",
        scope_id=session_id,
        content=episodic_content,
        title=f"Task: {task['goal'][:80]}",
        source_task_id=task_id,
        confidence=0.9 if verification_status == "pass" else 0.5,
    )

    # 2. If task passed verification and has reusable patterns → semantic
    if verification_status == "pass" and plan:
        semantic = _extract_semantic(task, plan, evidence)
        if semantic:
            result["semantic_written"] = mem.write_memory(
                db,
                memory_type="semantic",
                scope_id=session_id,
                content=semantic,
                title=f"Pattern: {task['task_type']} — {task['goal'][:50]}",
                source_task_id=task_id,
                confidence=0.85,
            )

    # 3. Check if this is a skill promotion candidate
    if verification_status == "pass" and _is_skill_candidate(task, evidence):
        result["skill_candidate"] = True

    # 4. Demote stale memories
    result["stale_demoted"] = _demote_stale(db, session_id)

    logger.debug(
        "Curated task %s: episodic=%s semantic=%s skill_candidate=%s stale_demoted=%d",
        task_id, bool(result["episodic_written"]), bool(result["semantic_written"]),
        result["skill_candidate"], result["stale_demoted"],
    )
    return result


# ── Content Builders ──────────────────────────────────────────────


def _build_episodic_content(
    task: dict, evidence: list[dict], verification_status: str,
) -> dict[str, Any]:
    """Build episodic memory content from a task."""
    tools_used = list({
        e["tool_name"] for e in evidence
        if e.get("tool_name")
    })

    return {
        "task_type": task.get("task_type", "general"),
        "goal": task["goal"][:500],
        "status": task["status"],
        "verification": verification_status,
        "tools_used": tools_used,
        "evidence_count": len(evidence),
        "failure_reason": task.get("failure_reason"),
        "duration_s": (
            (task["completed_at"] - task["started_at"])
            if task.get("completed_at") and task.get("started_at")
            else None
        ),
        "retry_count": task.get("retry_count", 0),
    }


def _extract_semantic(
    task: dict, plan: dict, evidence: list[dict],
) -> Optional[dict[str, Any]]:
    """Extract semantic knowledge from a successful task."""
    # Only extract if there are clear patterns
    tools_used = list({
        e["tool_name"] for e in evidence if e.get("tool_name")
    })
    if not tools_used:
        return None

    return {
        "task_type": task.get("task_type", "general"),
        "successful_tools": tools_used,
        "criteria_count": len(plan.get("success_criteria", [])),
        "subtask_count": len(plan.get("subtasks", [])),
        "pattern": f"{task['task_type']} tasks work well with: {', '.join(tools_used)}",
    }


def _is_skill_candidate(task: dict, evidence: list[dict]) -> bool:
    """Determine if a task should be considered for skill promotion."""
    # Requirements: passed, had tool usage, wasn't trivial
    if task.get("retry_count", 0) > 1:
        return False  # Too many retries = unstable pattern
    if len(evidence) < 2:
        return False  # Too little evidence
    tools = {e["tool_name"] for e in evidence if e.get("tool_name")}
    if not tools:
        return False  # No tool-based work
    return True


# ── Stale Memory Demotion ─────────────────────────────────────────


def _demote_stale(db: Any, scope_id: str, max_age_days: float = 30) -> int:
    """Deactivate episodic memories older than max_age_days with low freshness."""
    cutoff = time.time() - (max_age_days * 86400)
    count = 0

    try:
        rows = db._conn.execute(
            """SELECT id, freshness_score, confidence FROM memory_records
               WHERE scope_id = ? AND memory_type = 'episodic'
               AND is_active = 1 AND created_at < ?
               AND freshness_score < 0.3 AND confidence < 0.6""",
            (scope_id, cutoff),
        ).fetchall()

        for r in rows:
            mem.deactivate(db, r["id"], reason="stale_demotion")
            count += 1
    except Exception as e:
        logger.debug("Stale demotion error (non-fatal): %s", e)

    return count


# ── Conflict Detection ────────────────────────────────────────────


def detect_conflicts(
    db: Any,
    scope_id: str,
    memory_type: str = "semantic",
) -> list[tuple[dict, dict]]:
    """
    Find potentially conflicting memories of the same type.

    Returns pairs of (older, newer) records with overlapping titles/content.
    Phase 1: simple title-based matching only.
    """
    records = mem.get_memories_by_type(db, memory_type, scope_id, limit=100)
    conflicts = []

    for i, a in enumerate(records):
        for b in records[i + 1:]:
            if _titles_overlap(a.get("title", ""), b.get("title", "")):
                # Order: older first
                if a["created_at"] <= b["created_at"]:
                    conflicts.append((a, b))
                else:
                    conflicts.append((b, a))

    return conflicts


def _titles_overlap(a: str, b: str) -> bool:
    """Check if two titles are similar enough to be conflicting."""
    if not a or not b:
        return False
    a_words = set(a.lower().split()) - {"task:", "pattern:", "the", "a", "an"}
    b_words = set(b.lower().split()) - {"task:", "pattern:", "the", "a", "an"}
    if not a_words or not b_words:
        return False
    overlap = len(a_words & b_words) / min(len(a_words), len(b_words))
    return overlap > 0.6
