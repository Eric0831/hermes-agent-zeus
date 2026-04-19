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
            proposals.append({
                "proposal_type": "new_verifier_pattern",
                "title": f"Address low verification pass rate for '{task_family}'",
                "detail": detail,
                "expected_gain": 0.4,
                "risk_score": 0.25,
            })

        if finding_type == "verification_too_lenient":
            proposals.append({
                "proposal_type": "new_verifier_pattern",
                "title": f"Tighten verification criteria for '{task_family}'",
                "detail": data.get("detail", {}),
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
                "expected_gain": 0.25,
                "risk_score": 0.15,
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
