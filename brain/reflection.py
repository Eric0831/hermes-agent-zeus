"""Reflection Pipeline — structured task postmortems and policy updates.

After each task, generates a structured reflection:
  - What worked / what failed
  - Root cause classification
  - Reusable patterns
  - Policy update recommendations

Phase 1: rule-based reflection. Phase 2 can add LLM-based deep reflection.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _rid() -> str:
    return f"refl_{uuid.uuid4().hex[:12]}"


# ── Root Cause Classes ────────────────────────────────────────────

ROOT_CAUSE_CLASSES = (
    "success",              # Task completed successfully
    "insufficient_evidence",  # Lacked proof of completion
    "tool_failure",         # A tool call failed
    "planning_mismatch",    # Plan didn't match actual execution needs
    "timeout",              # Exceeded time/token budget
    "model_error",          # LLM produced invalid/unhelpful output
    "external_dependency",  # External service/resource issue
    "unknown",              # Couldn't determine
)


# ── Public API ────────────────────────────────────────────────────


def generate_reflection(
    db: Any,
    task: dict[str, Any],
    evidence: list[dict[str, Any]],
    verification: Optional[dict[str, Any]] = None,
    plan: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Generate a structured reflection for a completed task.

    Returns the reflection dict and persists it to the DB.
    """
    task_id = task["id"]
    task_family = task.get("task_type", "general")

    # Analyze what happened
    what_worked = _extract_what_worked(task, evidence, verification)
    what_failed = _extract_what_failed(task, evidence, verification)
    root_cause = _classify_root_cause(task, evidence, verification)
    reusable_patterns = _extract_patterns(task, evidence, plan)
    policy_deltas = _suggest_policy_deltas(task, evidence, verification, root_cause)

    reflection = {
        "task_id": task_id,
        "task_type": task_family,
        "goal": task.get("goal", "")[:500],
        "outcome": task.get("status"),
        "what_worked": what_worked,
        "what_failed": what_failed,
        "root_cause_class": root_cause,
        "reusable_patterns": reusable_patterns,
        "policy_deltas": policy_deltas,
        "evidence_count": len(evidence),
        "retry_count": task.get("retry_count", 0),
        "duration_s": (
            (task["completed_at"] - task["started_at"])
            if task.get("completed_at") and task.get("started_at")
            else None
        ),
    }

    # Confidence based on evidence quality
    confidence = _compute_confidence(task, evidence, verification)

    # Persist
    rid = _rid()
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO reflections
               (id, task_id, task_family, reflection_json, root_cause_class,
                confidence, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (rid, task_id, task_family,
             json.dumps(reflection, ensure_ascii=False, default=str),
             root_cause, confidence, now),
        )
    db._execute_write(_do)

    reflection["id"] = rid
    reflection["confidence"] = confidence
    logger.info("Reflection %s: task=%s outcome=%s root_cause=%s",
                rid, task_id, task["status"], root_cause)
    return reflection


def get_reflections_for_family(
    db: Any,
    task_family: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Get recent reflections for a task family."""
    rows = db._conn.execute(
        """SELECT id, task_id, task_family, reflection_json,
                  root_cause_class, confidence, created_at
           FROM reflections
           WHERE task_family = ?
           ORDER BY created_at DESC LIMIT ?""",
        (task_family, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_reflection(db: Any, reflection_id: str) -> Optional[dict[str, Any]]:
    """Get a single reflection by ID."""
    row = db._conn.execute(
        "SELECT * FROM reflections WHERE id = ?", (reflection_id,)
    ).fetchone()
    return dict(row) if row else None


def get_family_insights(db: Any, task_family: str) -> dict[str, Any]:
    """
    Aggregate insights across reflections for a task family.

    Returns patterns, common failures, and success factors.
    """
    reflections = get_reflections_for_family(db, task_family, limit=50)
    if not reflections:
        return {"family": task_family, "total": 0}

    root_causes: dict[str, int] = {}
    total_patterns: list[str] = []
    total_deltas: list[dict] = []
    success_count = 0
    failure_count = 0

    for r in reflections:
        rc = r.get("root_cause_class", "unknown")
        root_causes[rc] = root_causes.get(rc, 0) + 1

        try:
            data = json.loads(r.get("reflection_json", "{}"))
            total_patterns.extend(data.get("reusable_patterns", []))
            total_deltas.extend(data.get("policy_deltas", []))
            if data.get("outcome") == "completed":
                success_count += 1
            elif data.get("outcome") == "failed":
                failure_count += 1
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "family": task_family,
        "total": len(reflections),
        "success_count": success_count,
        "failure_count": failure_count,
        "root_cause_distribution": root_causes,
        "common_patterns": list(set(total_patterns))[:10],
        "suggested_deltas": total_deltas[:5],
    }


# ── Analysis Helpers ──────────────────────────────────────────────


def _extract_what_worked(task, evidence, verification) -> list[str]:
    worked = []
    tools = {e["tool_name"] for e in evidence if e.get("tool_name")}
    if tools:
        worked.append(f"Tools used successfully: {', '.join(sorted(tools))}")
    if len(evidence) >= 3:
        worked.append(f"Collected {len(evidence)} evidence records")
    if verification and verification.get("status") == "pass":
        worked.append("All criteria verified")
    if task.get("retry_count", 0) == 0 and task.get("status") == "completed":
        worked.append("Completed on first attempt")
    return worked


def _extract_what_failed(task, evidence, verification) -> list[str]:
    failed = []
    if task.get("status") == "failed":
        if task.get("failure_reason"):
            failed.append(f"Failure: {task['failure_reason'][:200]}")
    if verification:
        unmet = [
            c for c in verification.get("criteria_results", [])
            if c.get("status") == "unmet"
        ]
        for c in unmet[:3]:
            failed.append(f"Unmet criterion: {c.get('description', '?')[:100]}")
        if verification.get("missing_evidence"):
            failed.append(f"Missing evidence: {', '.join(verification['missing_evidence'][:3])}")
    if task.get("retry_count", 0) > 0:
        failed.append(f"Required {task['retry_count']} retries")
    if not evidence:
        failed.append("No tool-based evidence collected")
    return failed


def _classify_root_cause(task, evidence, verification) -> str:
    if task.get("status") == "completed":
        return "success"

    failure = (task.get("failure_reason") or "").lower()
    if "timeout" in failure or "budget" in failure:
        return "timeout"
    if "tool" in failure or "error" in failure:
        return "tool_failure"

    if not evidence:
        return "insufficient_evidence"

    if verification:
        missing = verification.get("missing_evidence", [])
        if len(missing) == len(verification.get("criteria_results", [])):
            return "planning_mismatch"
        if missing:
            return "insufficient_evidence"

    return "unknown"


def _extract_patterns(task, evidence, plan) -> list[str]:
    patterns = []
    tools = sorted({e["tool_name"] for e in evidence if e.get("tool_name")})

    if tools and task.get("status") == "completed":
        patterns.append(
            f"{task.get('task_type', 'general')} tasks: "
            f"tool chain [{' → '.join(tools)}] works well"
        )

    if plan and plan.get("subtasks"):
        n = len(plan["subtasks"])
        patterns.append(f"Task decomposed into {n} subtasks")

    if task.get("retry_count", 0) == 0 and task.get("status") == "completed":
        patterns.append("First-attempt success — plan was well-matched")

    return patterns


def _suggest_policy_deltas(task, evidence, verification, root_cause) -> list[dict]:
    deltas = []

    if root_cause == "timeout":
        deltas.append({
            "type": "planner_policy",
            "target": task.get("task_type", "general"),
            "suggestion": "increase_budget",
            "reason": "Task timed out",
        })

    if root_cause == "insufficient_evidence":
        deltas.append({
            "type": "verifier_policy",
            "target": task.get("task_type", "general"),
            "suggestion": "add_evidence_requirements",
            "reason": "Task lacked sufficient evidence",
        })

    if root_cause == "planning_mismatch":
        deltas.append({
            "type": "planner_policy",
            "target": task.get("task_type", "general"),
            "suggestion": "revise_decomposition",
            "reason": "Plan subtasks didn't align with actual execution",
        })

    return deltas


def _compute_confidence(task, evidence, verification) -> float:
    """Compute reflection confidence based on data quality."""
    score = 0.5

    # More evidence = higher confidence
    if len(evidence) >= 3:
        score += 0.2
    elif len(evidence) >= 1:
        score += 0.1

    # Verification result increases confidence
    if verification:
        score += 0.15

    # Completed tasks are more reliable reflections
    if task.get("status") == "completed":
        score += 0.1

    return min(1.0, score)
