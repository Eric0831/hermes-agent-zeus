"""Evolution Architect — generate capability proposals from meta-learning.

Analyzes meta_learning_findings and reflections for a task family to
identify gaps (recurring failures, missing skill coverage, verification
weaknesses). Generates capability_proposals that feed into the
capability_manager lifecycle.

Proposal types:
  - new_verifier_pattern: better verification strategies
  - new_decomposition: improved task decomposition approaches
  - new_skill_family: entirely new skill category needed
  - new_routing_doctrine: changed routing/priority rules
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

PROPOSAL_TYPES = (
    "new_verifier_pattern",
    "new_decomposition",
    "new_skill_family",
    "new_routing_doctrine",
)

PROPOSAL_STATUSES = (
    "proposed",
    "constitutional_checked",
    "governance_reviewed",
    "approved",
    "rejected",
    "incubating",
    "adopted",
    "retired",
)


def _proposal_id() -> str:
    return f"cprop_{uuid.uuid4().hex[:12]}"


# ── Public API ────────────────────────────────────────────────────


def generate_proposals(
    db: Any,
    task_family: str,
    *,
    source_run_id: Optional[str] = None,
) -> list[str]:
    """Analyze meta-learning findings + reflections for a task family.

    Identifies gaps and generates capability_proposals.
    Returns list of proposal IDs created.
    """
    findings = _get_findings_for_family(db, task_family)
    reflections = _get_reflections_for_family(db, task_family)
    skills = _get_skills_for_family(db, task_family)

    proposals: list[dict] = []

    # Analyze recurring failures from reflections
    proposals.extend(_analyze_failure_patterns(task_family, reflections))

    # Analyze verification weaknesses from findings
    proposals.extend(_analyze_verification_gaps(task_family, findings))

    # Analyze skill coverage gaps
    proposals.extend(_analyze_skill_gaps(task_family, findings, skills))

    # Analyze routing/decomposition issues
    proposals.extend(_analyze_routing_issues(task_family, findings, reflections))

    # Analyze positive signals (fast/evidence_rich/high_performing_tool)
    proposals.extend(_analyze_positive_signals(task_family, findings, skills))

    if not proposals:
        logger.info(
            "[EvolutionArchitect] No proposals generated for family '%s'",
            task_family,
        )
        return []

    # Persist proposals — dedupe against existing active proposals so
    # repeated runs don't flood the table with identical entries. An
    # existing (type, family, title) in any non-terminal state is
    # considered covered; rejected / deprecated ones can re-emerge.
    _ACTIVE_PROPOSAL_STATES = ("proposed", "approved", "incubating", "active")
    existing_rows = db._conn.execute(
        f"""SELECT proposal_type, title FROM capability_proposals
            WHERE target_task_family = ?
              AND status IN ({','.join('?' * len(_ACTIVE_PROPOSAL_STATES))})""",
        (task_family, *_ACTIVE_PROPOSAL_STATES),
    ).fetchall()
    existing_keys: set[tuple[str, str]] = {
        (r["proposal_type"] if hasattr(r, "keys") else r[0],
         r["title"] if hasattr(r, "keys") else r[1])
        for r in existing_rows
    }

    now = time.time()
    ids: list[str] = []
    skipped = 0
    for p in proposals:
        key = (p["proposal_type"], p["title"])
        if key in existing_keys:
            skipped += 1
            continue
        existing_keys.add(key)  # dedupe within this batch too
        pid = _proposal_id()
        ids.append(pid)

        def _do(conn, _p=p, _pid=pid):
            conn.execute(
                """INSERT INTO capability_proposals
                   (id, proposal_type, target_task_family, title,
                    proposal_json, expected_gain, risk_score,
                    source_run_id, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _pid,
                    _p["proposal_type"],
                    task_family,
                    _p["title"],
                    json.dumps(_p, ensure_ascii=False, default=str),
                    _p.get("expected_gain", 0.0),
                    _p.get("risk_score", 0.3),
                    source_run_id,
                    "proposed",
                    now,
                    now,
                ),
            )

        db._execute_write(_do)

    if skipped:
        logger.debug(
            "[EvolutionArchitect] Deduped %d already-active proposals for '%s'",
            skipped, task_family,
        )
    logger.info(
        "[EvolutionArchitect] Generated %d proposals for family '%s' (skipped %d duplicates)",
        len(ids), task_family, skipped,
    )
    return ids


def get_proposal(db: Any, proposal_id: str) -> Optional[dict]:
    """Get a single proposal by ID."""
    row = db._conn.execute(
        "SELECT * FROM capability_proposals WHERE id = ?",
        (proposal_id,),
    ).fetchone()
    return dict(row) if row else None


def get_proposals(
    db: Any,
    *,
    task_family: Optional[str] = None,
    status: str = "proposed",
    limit: int = 20,
) -> list[dict]:
    """Get proposals, optionally filtered by task family and status."""
    if task_family:
        rows = db._conn.execute(
            """SELECT * FROM capability_proposals
               WHERE target_task_family = ? AND status = ?
               ORDER BY created_at DESC LIMIT ?""",
            (task_family, status, limit),
        ).fetchall()
    else:
        rows = db._conn.execute(
            """SELECT * FROM capability_proposals
               WHERE status = ?
               ORDER BY created_at DESC LIMIT ?""",
            (status, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def update_proposal_status(
    db: Any,
    proposal_id: str,
    status: str,
    reason: str = "",
) -> None:
    """Update a proposal's status."""
    if status not in PROPOSAL_STATUSES:
        raise ValueError(f"Invalid proposal status: {status}")

    now = time.time()

    def _do(conn):
        conn.execute(
            """UPDATE capability_proposals
               SET status = ?, updated_at = ?
               WHERE id = ?""",
            (status, now, proposal_id),
        )

    db._execute_write(_do)
    logger.info(
        "[EvolutionArchitect] Proposal %s -> %s (reason: %s)",
        proposal_id, status, reason or "none",
    )


# ── Data Gathering ───────────────────────────────────────────────


def _get_findings_for_family(db: Any, task_family: str) -> list[dict]:
    """Get meta-learning findings for a task family."""
    try:
        rows = db._conn.execute(
            """SELECT * FROM meta_learning_findings
               WHERE task_family = ?
               ORDER BY impact_score DESC LIMIT 50""",
            (task_family,),
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
    """Get active skills for a task family."""
    try:
        rows = db._conn.execute(
            """SELECT * FROM skill_registry
               WHERE task_family = ? AND status = 'active'
               ORDER BY success_rate DESC""",
            (task_family,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ── Analysis Functions ───────────────────────────────────────────


def _analyze_failure_patterns(
    task_family: str,
    reflections: list[dict],
) -> list[dict]:
    """Identify recurring failure patterns from reflections."""
    proposals: list[dict] = []
    if not reflections:
        return proposals

    # Count root causes
    root_causes: dict[str, int] = {}
    for r in reflections:
        rc = r.get("root_cause_class", "unknown")
        root_causes[rc] = root_causes.get(rc, 0) + 1

    total = len(reflections)

    # High verification failure rate => new verifier pattern
    insufficient = root_causes.get("insufficient_evidence", 0)
    if insufficient >= 3 and insufficient / total > 0.3:
        proposals.append({
            "proposal_type": "new_verifier_pattern",
            "title": f"Improve evidence collection for '{task_family}' tasks",
            "detail": {
                "root_cause": "insufficient_evidence",
                "occurrences": insufficient,
                "total_reflections": total,
                "rate": insufficient / total,
            },
            "suggestion": (
                f"{insufficient}/{total} reflections cite 'insufficient_evidence' "
                f"({insufficient / total:.0%}). Review the Planner prompt for "
                f"'{task_family}' tasks and add explicit tool-usage criteria "
                f"(e.g. 'call session_search at least once', 'summarize any "
                f"web_extract result before concluding'). Also consider tightening "
                f"the Verifier's heuristic for this family to require specific "
                f"evidence types."
            ),
            "action_hint": {
                "kind": "update_planner_prompt",
                "task_family": task_family,
                "focus": "mandatory_evidence_criteria",
            },
            "expected_gain": min(0.3 + (insufficient / total) * 0.4, 0.8),
            "risk_score": 0.2,
        })

    # High planning mismatch => new decomposition
    mismatch = root_causes.get("planning_mismatch", 0)
    if mismatch >= 3 and mismatch / total > 0.25:
        proposals.append({
            "proposal_type": "new_decomposition",
            "title": f"Revise decomposition strategy for '{task_family}' tasks",
            "detail": {
                "root_cause": "planning_mismatch",
                "occurrences": mismatch,
                "total_reflections": total,
                "rate": mismatch / total,
            },
            "suggestion": (
                f"{mismatch}/{total} reflections cite 'planning_mismatch' "
                f"({mismatch / total:.0%}). The Planner's subtask graph for "
                f"'{task_family}' is drifting from actual execution. Capture the "
                f"5 most recent successful '{task_family}' tasks, extract their "
                f"real subtask sequences, and inject them into the Planner's "
                f"few-shot examples for this family. Cap subtasks at 6 to reduce "
                f"over-decomposition."
            ),
            "action_hint": {
                "kind": "update_planner_examples",
                "task_family": task_family,
                "source": "successful_tasks_last_5",
            },
            "expected_gain": min(0.2 + (mismatch / total) * 0.5, 0.7),
            "risk_score": 0.3,
        })

    return proposals


def _analyze_verification_gaps(
    task_family: str,
    findings: list[dict],
) -> list[dict]:
    """Identify verification weaknesses from meta-learning findings."""
    proposals: list[dict] = []
    if not findings:
        return proposals

    for f in findings:
        try:
            data = json.loads(f.get("finding_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            continue

        finding_type = data.get("type", "")

        if finding_type == "low_verification_rate":
            detail = data.get("detail", {})
            rate = detail.get("rate", 0)
            proposals.append({
                "proposal_type": "new_verifier_pattern",
                "title": f"Address low verification pass rate for '{task_family}'",
                "detail": detail,
                "suggestion": (
                    f"Verification passes only {rate:.0%} of the time on "
                    f"'{task_family}'. Two likely causes: (1) Planner criteria are "
                    f"too ambitious relative to actual evidence captured, or "
                    f"(2) Verifier heuristics mis-classify evidence. Start by "
                    f"enabling verifier.verify_with_llm=true for this family in "
                    f"config.yaml for 3 days, then inspect the LLM failures and "
                    f"backport stricter or looser heuristic rules as needed."
                ),
                "action_hint": {
                    "kind": "toggle_verifier_llm",
                    "task_family": task_family,
                    "duration_days": 3,
                },
                "expected_gain": 0.4,
                "risk_score": 0.25,
            })

        if finding_type == "verification_too_lenient":
            proposals.append({
                "proposal_type": "new_verifier_pattern",
                "title": f"Tighten verification criteria for '{task_family}'",
                "detail": data.get("detail", {}),
                "suggestion": (
                    f"Verification passes >95% on '{task_family}' — the heuristic "
                    f"is likely pattern-matching keywords instead of actual work. "
                    f"Add at least one criterion type that demands concrete "
                    f"evidence (a specific tool_call, a file diff, a numeric "
                    f"result) and re-run meta_learning after 50 fresh tasks."
                ),
                "action_hint": {
                    "kind": "add_verifier_criterion_type",
                    "task_family": task_family,
                    "requires": "concrete_evidence_artifact",
                },
                "expected_gain": 0.2,
                "risk_score": 0.15,
            })

    return proposals


def _analyze_skill_gaps(
    task_family: str,
    findings: list[dict],
    skills: list[dict],
) -> list[dict]:
    """Identify missing skill coverage."""
    proposals: list[dict] = []

    # If underperforming family finding exists but no active skills
    underperforming = [
        f for f in findings
        if _finding_type(f) == "underperforming_family"
    ]

    if underperforming and not skills:
        proposals.append({
            "proposal_type": "new_skill_family",
            "title": f"Create skill family for underperforming '{task_family}' tasks",
            "detail": {
                "reason": "No active skills exist for an underperforming task family",
                "findings_count": len(underperforming),
            },
            "suggestion": (
                f"'{task_family}' is under-performing with zero active skills in "
                f"skill_registry. Pick the 3 most successful completed "
                f"'{task_family}' tasks from state.db, extract the common tool "
                f"sequence (session_search → X → Y → response), and register it "
                f"as a candidate skill named '{task_family}_default_flow'. "
                f"Promote to active after 5 successful invocations."
            ),
            "action_hint": {
                "kind": "extract_skill",
                "task_family": task_family,
                "source": "top_3_successful_tasks",
                "skill_name": f"{task_family}_default_flow",
            },
            "expected_gain": 0.5,
            "risk_score": 0.3,
        })

    # Low tool success findings suggest new skills needed
    low_tool = [
        f for f in findings
        if _finding_type(f) == "low_tool_success"
    ]
    if low_tool:
        proposals.append({
            "proposal_type": "new_skill_family",
            "title": f"Develop alternative tool strategies for '{task_family}'",
            "detail": {
                "reason": "Tools used by this family have low success correlation",
                "findings_count": len(low_tool),
            },
            "suggestion": (
                f"One or more tools used by '{task_family}' show <50% success "
                f"correlation. For each flagged tool in the detail payload, add "
                f"a fallback in the relevant brain.policy rule (retry with "
                f"alternative tool on failure) and document the preferred "
                f"alternative in a skill named '{task_family}_tool_fallbacks'."
            ),
            "action_hint": {
                "kind": "add_tool_fallback",
                "task_family": task_family,
                "skill_name": f"{task_family}_tool_fallbacks",
            },
            "expected_gain": 0.35,
            "risk_score": 0.25,
        })

    return proposals


def _analyze_routing_issues(
    task_family: str,
    findings: list[dict],
    reflections: list[dict],
) -> list[dict]:
    """Identify routing and prioritisation issues."""
    proposals: list[dict] = []

    # High retry rate suggests routing or planning issues
    high_retry = [
        f for f in findings
        if _finding_type(f) == "high_retry_rate"
    ]

    if high_retry:
        proposals.append({
            "proposal_type": "new_routing_doctrine",
            "title": f"Optimise routing for '{task_family}' (high retry rate)",
            "detail": {
                "reason": "High retry rate indicates suboptimal routing or priority",
                "findings_count": len(high_retry),
            },
            "suggestion": (
                f"'{task_family}' retries >30% of tasks. Plans are either "
                f"under-specified or the Verifier is too strict on the first "
                f"pass. Short-term: bump brain.max_retries from 2 → 3 only for "
                f"this family. Medium-term: add a 'clarification subtask' to the "
                f"Planner template so the first pass gathers requirements "
                f"before committing to execution."
            ),
            "action_hint": {
                "kind": "increase_retry_budget",
                "task_family": task_family,
                "new_max_retries": 3,
            },
            "expected_gain": 0.3,
            "risk_score": 0.2,
        })

    # Check reflections for timeout patterns
    if reflections:
        timeout_count = sum(
            1 for r in reflections
            if r.get("root_cause_class") == "timeout"
        )
        if timeout_count >= 3 and timeout_count / len(reflections) > 0.2:
            proposals.append({
                "proposal_type": "new_routing_doctrine",
                "title": f"Adjust budgets for '{task_family}' (frequent timeouts)",
                "detail": {
                    "timeout_count": timeout_count,
                    "total_reflections": len(reflections),
                    "rate": timeout_count / len(reflections),
                },
                "suggestion": (
                    f"{timeout_count}/{len(reflections)} reflections cite "
                    f"'timeout' ({timeout_count / len(reflections):.0%}) on "
                    f"'{task_family}'. Raise task budget_ms by 50% for this "
                    f"family in the Planner default, or split the family into "
                    f"sub-types (fast_{task_family} vs deep_{task_family}) with "
                    f"separate budgets."
                ),
                "action_hint": {
                    "kind": "increase_task_budget",
                    "task_family": task_family,
                    "budget_multiplier": 1.5,
                },
                "expected_gain": 0.25,
                "risk_score": 0.15,
            })

    return proposals


def _analyze_positive_signals(
    task_family: str,
    findings: list[dict],
    skills: list[dict],
) -> list[dict]:
    """Turn healthy-system findings into promotion / extraction proposals.

    Phase 0 meta-learning analyzers only fired on negative patterns, so a
    well-running system produced zero proposals. This matches positive
    signals (fast_family, evidence_rich_family, high_performing_tool) to
    concrete 'preserve this pattern' actions.
    """
    proposals: list[dict] = []

    fast = [f for f in findings if _finding_type(f) == "fast_family"]
    if fast and not skills:
        detail = {}
        try:
            detail = json.loads(fast[0].get("finding_json", "{}")).get("detail", {})
        except Exception:
            pass
        median = detail.get("median_duration_s", 0)
        proposals.append({
            "proposal_type": "new_skill_family",
            "title": f"Lock in fast-path template for '{task_family}'",
            "detail": {
                "reason": "Family is unusually fast — preserve its plan as a template",
                "median_duration_s": median,
                "findings_count": len(fast),
            },
            "suggestion": (
                f"'{task_family}' completes in {median:.0f}s (median), faster "
                f"than peer families. Capture the subtask sequence of the top "
                f"10 fastest successful '{task_family}' tasks, extract the "
                f"common tool chain, and register it as a candidate skill "
                f"'{task_family}_fast_path'. Promote to active after 3 successful "
                f"invocations on fresh tasks."
            ),
            "action_hint": {
                "kind": "extract_skill",
                "task_family": task_family,
                "source": "top_10_fastest_successful_tasks",
                "skill_name": f"{task_family}_fast_path",
            },
            "expected_gain": 0.30,
            "risk_score": 0.15,
        })

    rich = [f for f in findings if _finding_type(f) == "evidence_rich_family"]
    if rich:
        detail = {}
        try:
            detail = json.loads(rich[0].get("finding_json", "{}")).get("detail", {})
        except Exception:
            pass
        avg = detail.get("avg_evidence_per_task", 0)
        proposals.append({
            "proposal_type": "new_skill_family",
            "title": f"Extract precedents from evidence-rich '{task_family}'",
            "detail": {
                "reason": "Family accumulates rich evidence — fertile for precedent extraction",
                "avg_evidence_per_task": avg,
                "findings_count": len(rich),
            },
            "suggestion": (
                f"'{task_family}' averages {avg:.0f} evidence/task — significantly "
                f"above peer families. Run brain.precedent_store on the 10 most "
                f"recent completed '{task_family}' tasks to extract reusable "
                f"decision precedents, then expose them to the Planner via its "
                f"context so future '{task_family}' tasks can shortcut to known-"
                f"good patterns."
            ),
            "action_hint": {
                "kind": "extract_precedents",
                "task_family": task_family,
                "source": "top_10_recent_completed_tasks",
            },
            "expected_gain": 0.40,
            "risk_score": 0.10,
        })

    high_perf = [f for f in findings if _finding_type(f) == "high_performing_tool"]
    if high_perf:
        tools = []
        for f in high_perf:
            try:
                d = json.loads(f.get("finding_json", "{}")).get("detail", {})
                if d.get("tool"):
                    tools.append(d["tool"])
            except Exception:
                pass
        tools_str = ", ".join(tools[:5]) if tools else "(see detail)"
        proposals.append({
            "proposal_type": "new_routing_doctrine",
            "title": f"Prefer high-performing tools ({tools_str[:40]}) in '{task_family}'",
            "detail": {
                "reason": "These tools show >=90% success correlation — make them the default path",
                "tools": tools,
                "findings_count": len(high_perf),
            },
            "suggestion": (
                f"Tools {tools_str} show >=90% success correlation on "
                f"'{task_family}'. Update the Planner's recommended_tools for "
                f"this family to put them first in the list so Policy-gated "
                f"alternatives are only tried when the preferred path is "
                f"unavailable. This tightens the fast path without removing "
                f"fallback capability."
            ),
            "action_hint": {
                "kind": "update_recommended_tools",
                "task_family": task_family,
                "prefer": tools,
            },
            "expected_gain": 0.35,
            "risk_score": 0.10,
        })

    return proposals


# ── Helpers ──────────────────────────────────────────────────────


def _finding_type(finding: dict) -> str:
    """Extract finding type from the finding_json field."""
    try:
        data = json.loads(finding.get("finding_json", "{}"))
        return data.get("type", "")
    except (json.JSONDecodeError, TypeError):
        return ""
