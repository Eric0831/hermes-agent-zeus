"""Recursive Reflection — multi-level reflection beyond individual tasks.

Goes beyond task-level reflection to capability-level and architecture-level
analysis. Identifies systemic patterns across the entire agent brain.

Reflection levels:
  - task: individual task postmortem (handled by reflection.py)
  - capability: effectiveness of a capability family over time
  - architecture: system-wide health and evolution readiness
  - governance: governance process effectiveness
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

REFLECTION_LEVELS = ("task", "capability", "architecture", "governance")


def _reflection_id() -> str:
    return f"rrefl_{uuid.uuid4().hex[:12]}"


# ── Public API ────────────────────────────────────────────────────


def reflect_on_capability(
    db: Any,
    capability_family: str,
    *,
    window_days: int = 30,
) -> dict:
    """Analyze all tasks, skills, and reflections for a capability family.

    Returns:
        {
            effectiveness_score: float (0.0-1.0),
            strengths: list[str],
            weaknesses: list[str],
            improvement_suggestions: list[str],
            should_evolve: bool,
        }
    """
    cutoff = time.time() - (window_days * 86400)

    tasks = _get_tasks_for_family(db, capability_family, cutoff)
    reflections = _get_reflections_for_family(db, capability_family)
    skills = _get_skills_for_family(db, capability_family)

    strengths: list[str] = []
    weaknesses: list[str] = []
    suggestions: list[str] = []

    # Task-level analysis
    if tasks:
        total = len(tasks)
        completed = sum(1 for t in tasks if t.get("status") == "completed")
        failed = sum(1 for t in tasks if t.get("status") == "failed")
        completion_rate = completed / total

        if completion_rate >= 0.8:
            strengths.append(
                f"High completion rate: {completion_rate:.0%} ({completed}/{total})"
            )
        elif completion_rate < 0.5:
            weaknesses.append(
                f"Low completion rate: {completion_rate:.0%} ({completed}/{total})"
            )
            suggestions.append(
                "Consider revising decomposition or verifier patterns"
            )

        # Retry analysis
        retry_tasks = [t for t in tasks if t.get("retry_count", 0) > 0]
        if retry_tasks and len(retry_tasks) / total > 0.3:
            weaknesses.append(
                f"High retry rate: {len(retry_tasks)}/{total} tasks needed retries"
            )
            suggestions.append(
                "Plans may be under-specified — add pre-checks or clearer steps"
            )
        elif total >= 5 and not retry_tasks:
            strengths.append("Zero retries across all tasks")

        # Duration analysis
        durations = _extract_durations(tasks)
        if durations:
            avg_d = sum(durations) / len(durations)
            if avg_d > 300:  # > 5 minutes average
                weaknesses.append(
                    f"Slow average execution: {avg_d:.0f}s per task"
                )
                suggestions.append(
                    "Consider parallel subtask execution or caching"
                )
            elif avg_d < 30 and total >= 5:
                strengths.append(f"Fast execution: {avg_d:.0f}s average")
    else:
        completion_rate = 0.0

    # Reflection-level analysis
    if reflections:
        root_causes = _count_root_causes(reflections)
        dominant_cause = max(root_causes, key=root_causes.get) if root_causes else None

        if dominant_cause and dominant_cause != "success":
            weaknesses.append(
                f"Dominant failure mode: '{dominant_cause}' "
                f"({root_causes[dominant_cause]} occurrences)"
            )
            suggestions.append(
                f"Focus improvement on addressing '{dominant_cause}' failures"
            )

        if root_causes.get("success", 0) > root_causes.get("unknown", 0):
            strengths.append("Root causes are well-classified")
    else:
        strengths.append("No failure reflections recorded (or no data yet)")

    # Skill-level analysis
    if skills:
        active_skills = [s for s in skills if s.get("status") == "active"]
        if active_skills:
            strengths.append(f"{len(active_skills)} active skills available")
        else:
            weaknesses.append("No active skills despite skill records existing")
            suggestions.append("Review and promote candidate skills")
    else:
        if tasks and len(tasks) >= 5:
            weaknesses.append("No skills extracted from completed tasks")
            suggestions.append(
                "Enable skill generation for successful task patterns"
            )

    # Compute overall score
    effectiveness_score = _compute_effectiveness(
        completion_rate, len(strengths), len(weaknesses), len(tasks),
    )

    should_evolve = (
        effectiveness_score < 0.6
        or len(weaknesses) > len(strengths)
        or (len(tasks) >= 10 and completion_rate < 0.7)
    )

    result = {
        "effectiveness_score": round(effectiveness_score, 3),
        "strengths": strengths,
        "weaknesses": weaknesses,
        "improvement_suggestions": suggestions,
        "should_evolve": should_evolve,
    }

    # Auto-save the reflection
    save_reflection(
        db,
        scope_type="capability",
        scope_id=capability_family,
        level="capability",
        reflection_data=result,
        confidence=min(0.5 + len(tasks) * 0.02, 0.95),
    )

    logger.info(
        "[RecursiveReflection] Capability '%s': score=%.3f evolve=%s",
        capability_family, effectiveness_score, should_evolve,
    )
    return result


def reflect_on_architecture(
    db: Any,
    *,
    window_days: int = 60,
) -> dict:
    """System-wide architectural reflection.

    Returns:
        {
            healthy_families: list[str],
            struggling_families: list[str],
            evolution_recommendations: list[str],
            governance_notes: list[str],
        }
    """
    cutoff = time.time() - (window_days * 86400)

    # Get all task families
    families = _get_all_families(db, cutoff)

    healthy: list[str] = []
    struggling: list[str] = []
    recommendations: list[str] = []
    governance_notes: list[str] = []

    if not families:
        return {
            "healthy_families": [],
            "struggling_families": [],
            "evolution_recommendations": [
                "No task data available — system may be newly deployed"
            ],
            "governance_notes": [],
        }

    for family, stats in families.items():
        total = stats["total"]
        completed = stats["completed"]
        rate = completed / total if total > 0 else 0.0

        if total < 3:
            continue  # not enough data

        if rate >= 0.7:
            healthy.append(family)
        else:
            struggling.append(family)
            recommendations.append(
                f"Family '{family}' has {rate:.0%} completion rate — "
                f"consider evolution proposals"
            )

    # Check governance health
    gov_stats = _get_governance_stats(db)
    if gov_stats:
        total_reviews = gov_stats.get("total", 0)
        rejected = gov_stats.get("rejected", 0)
        if total_reviews > 0 and rejected / total_reviews > 0.5:
            governance_notes.append(
                f"High rejection rate in governance: {rejected}/{total_reviews} "
                f"— proposals may be poorly aligned"
            )
        if total_reviews == 0:
            governance_notes.append(
                "No governance reviews recorded — "
                "ensure proposals are routed through governance"
            )

    # Check capability version health
    cap_stats = _get_capability_stats(db)
    if cap_stats:
        adopted = cap_stats.get("adopted", 0)
        deprecated = cap_stats.get("deprecated", 0)
        if adopted == 0 and deprecated > 0:
            recommendations.append(
                "No adopted capabilities despite deprecated versions — "
                "capability lifecycle may be stalled"
            )

    # Check constitution
    rules = _get_constitution_count(db)
    if rules == 0:
        governance_notes.append(
            "No constitution rules defined — seed default rules"
        )

    result = {
        "healthy_families": healthy,
        "struggling_families": struggling,
        "evolution_recommendations": recommendations,
        "governance_notes": governance_notes,
    }

    # Auto-save
    save_reflection(
        db,
        scope_type="system",
        scope_id="architecture",
        level="architecture",
        reflection_data=result,
        confidence=0.7,
    )

    logger.info(
        "[RecursiveReflection] Architecture: %d healthy, %d struggling",
        len(healthy), len(struggling),
    )
    return result


def save_reflection(
    db: Any,
    scope_type: str,
    scope_id: str,
    level: str,
    reflection_data: dict,
    confidence: float = 0.5,
) -> str:
    """Persist a reflection to the recursive_reflections table.

    Args:
        scope_type: e.g. 'capability', 'system', 'task'
        scope_id: identifier for the scope (family name, task id, etc.)
        level: 'task', 'capability', 'architecture', 'governance'
        reflection_data: the reflection payload
        confidence: 0.0-1.0

    Returns the reflection id.
    """
    if level not in REFLECTION_LEVELS:
        raise ValueError(f"Invalid reflection level: {level}")

    rid = _reflection_id()
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO recursive_reflections
               (id, scope_type, scope_id, reflection_level,
                reflection_json, confidence, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                rid,
                scope_type,
                scope_id,
                level,
                json.dumps(reflection_data, ensure_ascii=False, default=str),
                confidence,
                now,
            ),
        )

    db._execute_write(_do)
    return rid


def get_reflections(
    db: Any,
    scope_type: Optional[str] = None,
    scope_id: Optional[str] = None,
    level: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """Get reflections with optional filters."""
    conditions: list[str] = []
    params: list[Any] = []

    if scope_type is not None:
        conditions.append("scope_type = ?")
        params.append(scope_type)
    if scope_id is not None:
        conditions.append("scope_id = ?")
        params.append(scope_id)
    if level is not None:
        conditions.append("reflection_level = ?")
        params.append(level)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    rows = db._conn.execute(
        f"""SELECT * FROM recursive_reflections
            {where}
            ORDER BY created_at DESC LIMIT ?""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


# ── Data Gathering ───────────────────────────────────────────────


def _get_tasks_for_family(
    db: Any,
    task_family: str,
    cutoff: float,
) -> list[dict]:
    """Get tasks for a family within a time window."""
    try:
        rows = db._conn.execute(
            """SELECT id, task_type, status, verification_status,
                      retry_count, started_at, completed_at, created_at
               FROM tasks
               WHERE task_type = ? AND created_at >= ?
               ORDER BY created_at""",
            (task_family, cutoff),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _get_reflections_for_family(db: Any, task_family: str) -> list[dict]:
    """Get reflections for a task family."""
    try:
        rows = db._conn.execute(
            """SELECT * FROM reflections
               WHERE task_family = ?
               ORDER BY created_at DESC LIMIT 50""",
            (task_family,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _get_skills_for_family(db: Any, task_family: str) -> list[dict]:
    """Get skills for a task family."""
    try:
        rows = db._conn.execute(
            """SELECT * FROM skill_registry
               WHERE task_family = ?
               ORDER BY created_at DESC""",
            (task_family,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _get_all_families(db: Any, cutoff: float) -> dict[str, dict]:
    """Get per-family task stats."""
    try:
        rows = db._conn.execute(
            """SELECT task_type, status, COUNT(*) as cnt
               FROM tasks
               WHERE created_at >= ?
               GROUP BY task_type, status""",
            (cutoff,),
        ).fetchall()

        families: dict[str, dict] = {}
        for r in rows:
            fam = r["task_type"] or "general"
            if fam not in families:
                families[fam] = {"total": 0, "completed": 0, "failed": 0}
            families[fam]["total"] += r["cnt"]
            if r["status"] == "completed":
                families[fam]["completed"] += r["cnt"]
            elif r["status"] == "failed":
                families[fam]["failed"] += r["cnt"]

        return families
    except Exception:
        return {}


def _get_governance_stats(db: Any) -> dict:
    """Get governance review stats."""
    try:
        rows = db._conn.execute(
            """SELECT decision, COUNT(*) as cnt
               FROM governance_reviews
               GROUP BY decision""",
        ).fetchall()
        stats: dict[str, int] = {"total": 0}
        for r in rows:
            stats[r["decision"]] = r["cnt"]
            stats["total"] += r["cnt"]
        return stats
    except Exception:
        return {}


def _get_capability_stats(db: Any) -> dict:
    """Get capability version stats."""
    try:
        rows = db._conn.execute(
            """SELECT status, COUNT(*) as cnt
               FROM capability_versions
               GROUP BY status""",
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}
    except Exception:
        return {}


def _get_constitution_count(db: Any) -> int:
    """Count active constitution rules."""
    try:
        row = db._conn.execute(
            "SELECT COUNT(*) as cnt FROM constitution_rules WHERE is_active = 1",
        ).fetchone()
        return row["cnt"] if row else 0
    except Exception:
        return 0


# ── Analysis Helpers ─────────────────────────────────────────────


def _extract_durations(tasks: list[dict]) -> list[float]:
    """Extract valid durations from tasks."""
    durations: list[float] = []
    for t in tasks:
        started = t.get("started_at")
        ended = t.get("completed_at")
        if started and ended:
            try:
                d = float(ended) - float(started)
                if d > 0:
                    durations.append(d)
            except (TypeError, ValueError):
                pass
    return durations


def _count_root_causes(reflections: list[dict]) -> dict[str, int]:
    """Count root cause classes across reflections."""
    counts: dict[str, int] = {}
    for r in reflections:
        rc = r.get("root_cause_class", "unknown")
        counts[rc] = counts.get(rc, 0) + 1
    return counts


def _compute_effectiveness(
    completion_rate: float,
    strength_count: int,
    weakness_count: int,
    task_count: int,
) -> float:
    """Compute an overall effectiveness score."""
    if task_count == 0:
        return 0.5  # neutral when no data

    # Base: completion rate weighted heavily
    score = completion_rate * 0.6

    # Adjust for strength/weakness balance
    balance = strength_count - weakness_count
    score += max(-0.2, min(0.2, balance * 0.05))

    # Small bonus for having enough data
    if task_count >= 10:
        score += 0.1
    elif task_count >= 5:
        score += 0.05

    return max(0.0, min(1.0, score))
