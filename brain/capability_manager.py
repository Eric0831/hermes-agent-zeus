"""Capability Manager — version lifecycle for evolving capabilities.

Manages capability versions through a controlled lifecycle:
  proposed -> incubating -> experimental -> limited_rollout -> adopted

Each capability_family has at most one 'adopted' version at a time.
Adoption atomically deprecates the previous version (same pattern as
strategy.py's activate_strategy).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

CAPABILITY_STATUSES = (
    "proposed",
    "incubating",
    "experimental",
    "limited_rollout",
    "adopted",
    "deprecated",
    "retired",
)

# Valid forward transitions; any status can also go to deprecated/retired
# (except adopted needs governance approval for retirement).
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"incubating", "deprecated", "retired"},
    "incubating": {"experimental", "deprecated", "retired"},
    "experimental": {"limited_rollout", "deprecated", "retired"},
    "limited_rollout": {"adopted", "deprecated", "retired"},
    "adopted": {"deprecated"},  # retirement requires governance
    "deprecated": {"retired"},
    "retired": set(),
}


def _version_id() -> str:
    return f"capv_{uuid.uuid4().hex[:12]}"


# ── Public API ────────────────────────────────────────────────────


def create_version(
    db: Any,
    capability_family: str,
    definition: dict,
    *,
    source_proposal_id: Optional[str] = None,
    parent_version_id: Optional[str] = None,
) -> str:
    """Create a new capability version in 'proposed' status.

    Returns the version id.
    """
    vid = _version_id()
    now = time.time()
    version = _next_version(db, capability_family)

    def _do(conn):
        conn.execute(
            """INSERT INTO capability_versions
               (id, capability_family, version, status, definition_json,
                parent_version_id, source_proposal_id, created_at,
                activated_at, deprecated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                vid,
                capability_family,
                version,
                "proposed",
                json.dumps(definition, ensure_ascii=False, default=str),
                parent_version_id,
                source_proposal_id,
                now,
                None,
                None,
            ),
        )

    db._execute_write(_do)
    logger.info(
        "[CapabilityManager] Created %s v%d for family '%s'",
        vid, version, capability_family,
    )
    return vid


def transition_status(
    db: Any,
    version_id: str,
    new_status: str,
    reason: str = "",
) -> None:
    """Transition a capability version to a new status.

    Validates that the transition is allowed before applying.
    """
    if new_status not in CAPABILITY_STATUSES:
        raise ValueError(f"Invalid capability status: {new_status}")

    row = db._conn.execute(
        "SELECT * FROM capability_versions WHERE id = ?",
        (version_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Capability version not found: {version_id}")

    current = dict(row)
    current_status = current["status"]
    allowed = ALLOWED_TRANSITIONS.get(current_status, set())

    if new_status not in allowed:
        raise ValueError(
            f"Invalid transition: {current_status} -> {new_status} "
            f"(allowed: {allowed})"
        )

    now = time.time()

    def _do(conn):
        conn.execute(
            """UPDATE capability_versions
               SET status = ?, deprecated_at = ?
               WHERE id = ?""",
            (
                new_status,
                now if new_status in ("deprecated", "retired") else None,
                version_id,
            ),
        )

    db._execute_write(_do)
    logger.info(
        "[CapabilityManager] %s: %s -> %s (reason: %s)",
        version_id, current_status, new_status, reason or "none",
    )


def get_version(db: Any, version_id: str) -> Optional[dict]:
    """Get a capability version by ID."""
    row = db._conn.execute(
        "SELECT * FROM capability_versions WHERE id = ?",
        (version_id,),
    ).fetchone()
    return dict(row) if row else None


def promote_from_proposal(db: Any, proposal_id: str) -> str:
    """Bridge capability_proposals → capability_versions.

    Takes an 'approved' capability_proposal, creates a capability_version
    in 'incubating' status pre-linked via source_proposal_id, and moves
    the proposal's own status to 'incubating' so both sides stay in sync.

    The capability_family is derived as "{proposal_type}:{target_task_family}"
    so each (type, family) pair gets its own version sequence. Existing
    versions for the same family continue the sequence via _next_version.

    Raises ValueError when the proposal is missing, in the wrong status,
    or the transition fails.
    """
    from brain import evolution_architect

    p = evolution_architect.get_proposal(db, proposal_id)
    if not p:
        raise ValueError(f"Proposal not found: {proposal_id}")
    if p["status"] != "approved":
        raise ValueError(
            f"Proposal {proposal_id} is in status '{p['status']}' — "
            "only 'approved' can be promoted"
        )

    try:
        proposal_def = json.loads(p.get("proposal_json") or "{}")
    except Exception:
        proposal_def = {}

    capability_family = f"{p['proposal_type']}:{p['target_task_family']}"
    definition = {
        "proposal_type": p["proposal_type"],
        "target_task_family": p["target_task_family"],
        "title": p.get("title", ""),
        "expected_gain": p.get("expected_gain", 0.0),
        "risk_score": p.get("risk_score", 0.3),
        "source": proposal_def,
    }

    vid = create_version(
        db,
        capability_family=capability_family,
        definition=definition,
        source_proposal_id=proposal_id,
    )
    # Move the version forward from proposed to incubating (valid
    # transition per ALLOWED_TRANSITIONS).
    transition_status(db, vid, "incubating", reason="promoted_from_approved_proposal")

    # Keep the proposal in sync so it no longer appears in the 'approved'
    # backlog once it's actively being incubated as a version.
    evolution_architect.update_proposal_status(
        db, proposal_id, "incubating",
        reason=f"promoted_to_capability_version:{vid}",
    )
    logger.info(
        "[CapabilityManager] Promoted proposal %s -> version %s (family=%s)",
        proposal_id, vid, capability_family,
    )
    return vid


def get_active_version(db: Any, capability_family: str) -> Optional[dict]:
    """Get the currently adopted version for a capability family."""
    row = db._conn.execute(
        """SELECT * FROM capability_versions
           WHERE capability_family = ? AND status = 'adopted'
           LIMIT 1""",
        (capability_family,),
    ).fetchone()
    return dict(row) if row else None


def get_family_history(
    db: Any,
    capability_family: str,
    limit: int = 20,
) -> list[dict]:
    """Get version history for a capability family, newest first."""
    rows = db._conn.execute(
        """SELECT * FROM capability_versions
           WHERE capability_family = ?
           ORDER BY version DESC LIMIT ?""",
        (capability_family, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def adopt_version(db: Any, version_id: str) -> bool:
    """Atomically adopt a version: deprecate previous + activate new.

    The version must be in 'limited_rollout' status. Returns True on
    success, False if preconditions are not met.
    """
    try:
        row = db._conn.execute(
            "SELECT * FROM capability_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
        if not row:
            logger.warning("[CapabilityManager] Not found: %s", version_id)
            return False

        current = dict(row)
        if current["status"] != "limited_rollout":
            logger.warning(
                "[CapabilityManager] Cannot adopt %s — status is '%s' "
                "(must be 'limited_rollout')",
                version_id, current["status"],
            )
            return False

        now = time.time()
        family = current["capability_family"]

        def _do(conn):
            # Deprecate the current adopted version for this family
            conn.execute(
                """UPDATE capability_versions
                   SET status = 'deprecated', deprecated_at = ?
                   WHERE capability_family = ? AND status = 'adopted'""",
                (now, family),
            )
            # Adopt the new version
            conn.execute(
                """UPDATE capability_versions
                   SET status = 'adopted', activated_at = ?
                   WHERE id = ?""",
                (now, version_id),
            )

        db._execute_write(_do)
        logger.info(
            "[CapabilityManager] Adopted %s for family '%s' (v%s)",
            version_id, family, current["version"],
        )
        return True

    except Exception as e:
        logger.error("[CapabilityManager] adopt_version failed: %s", e)
        return False


def deprecate_version(db: Any, version_id: str) -> None:
    """Deprecate a capability version."""
    now = time.time()

    def _do(conn):
        conn.execute(
            """UPDATE capability_versions
               SET status = 'deprecated', deprecated_at = ?
               WHERE id = ?""",
            (now, version_id),
        )

    db._execute_write(_do)
    logger.info("[CapabilityManager] Deprecated %s", version_id)


def execute_action(db: Any, version_id: str) -> dict:
    """Execute a capability_version's embedded action_hint.

    Today only the 'extract_skill' kind is implemented end-to-end.
    Other kinds return a structured 'not_implemented' response so the
    operator knows the bookkeeping succeeded but the work is theirs.

    Returns a dict with:
        {"executed": bool, "kind": str, "result": ..., "note": str}

    Raises ValueError if the version doesn't exist or is in a
    state where executing its action_hint makes no sense
    (deprecated / retired).
    """
    v = get_version(db, version_id)
    if not v:
        raise ValueError(f"Capability version not found: {version_id}")
    if v["status"] in ("deprecated", "retired"):
        raise ValueError(
            f"Version {version_id} is {v['status']} — refusing to execute action"
        )

    try:
        definition = json.loads(v.get("definition_json") or "{}")
    except Exception:
        definition = {}
    source = definition.get("source") or {}
    action_hint = source.get("action_hint") or {}
    kind = action_hint.get("kind") or ""

    if not kind:
        return {
            "executed": False,
            "kind": "",
            "result": None,
            "note": "version has no action_hint — operator-guided only",
        }

    if kind == "extract_skill":
        result = _execute_extract_skill(db, v, action_hint)
        return {"executed": True, "kind": kind, "result": result, "note": ""}

    if kind == "extract_precedents":
        result = _execute_extract_precedents(db, v, action_hint)
        return {"executed": True, "kind": kind, "result": result, "note": ""}

    if kind == "update_recommended_tools":
        result = _execute_update_recommended_tools(db, v, action_hint)
        return {"executed": True, "kind": kind, "result": result, "note": ""}

    return {
        "executed": False,
        "kind": kind,
        "result": None,
        "note": (
            f"action_hint kind '{kind}' is recognized but not yet auto-executable. "
            "Operator should apply the suggestion manually; a future commit will "
            "wire the executor."
        ),
    }


def _execute_extract_skill(
    db: Any, version: dict, action_hint: dict,
) -> dict:
    """Synthesize a skill_registry candidate from the top successful
    tasks of the target family.

    Strategy:
      1. Pick up to 10 most recent completed tasks in the family.
      2. Gather each task's tool sequence from evidence_records
         (ordered by created_at).
      3. Compute the frequency-weighted union of tools used.
      4. Create a single skill_registry row, status='candidate',
         with source_task_id pointing at the most recent representative
         task and the source proposal/version referenced in the
         definition.

    Returns {"skill_id", "skill_name", "tasks_sampled", "tools": [...]}
    """
    family = action_hint.get("task_family") or version.get("capability_family", "").split(":", 1)[-1]
    skill_name = action_hint.get("skill_name") or f"{family}_auto_extracted"

    tasks = db._conn.execute(
        """SELECT id, task_type, goal, risk_level, completed_at
           FROM tasks
           WHERE status = 'completed' AND task_type = ?
           ORDER BY completed_at DESC LIMIT 10""",
        (family,),
    ).fetchall()
    if not tasks:
        return {
            "skill_id": None,
            "skill_name": skill_name,
            "tasks_sampled": 0,
            "tools": [],
            "note": f"no completed tasks for family '{family}' — nothing to extract",
        }

    tool_freq: dict[str, int] = {}
    ordered_tools_by_task: list[list[str]] = []
    for t in tasks:
        tid = t["id"] if hasattr(t, "keys") else t[0]
        seq = db._conn.execute(
            """SELECT tool_name FROM evidence_records
               WHERE task_id = ? AND tool_name IS NOT NULL
                     AND tool_name NOT IN ('', 'unknown')
               ORDER BY created_at""",
            (tid,),
        ).fetchall()
        tools_this_task = [
            (r["tool_name"] if hasattr(r, "keys") else r[0]) for r in seq
        ]
        if tools_this_task:
            ordered_tools_by_task.append(tools_this_task)
            for tn in tools_this_task:
                tool_freq[tn] = tool_freq.get(tn, 0) + 1

    if not tool_freq:
        return {
            "skill_id": None,
            "skill_name": skill_name,
            "tasks_sampled": len(tasks),
            "tools": [],
            "note": (
                f"sampled {len(tasks)} tasks but no named tool evidence — "
                "waiting for the c8cb2504 tool_name fix to accumulate data"
            ),
        }

    # Dominant tools: sort by frequency, take top ordered by first appearance
    ranked = sorted(tool_freq.items(), key=lambda kv: (-kv[1], kv[0]))
    core_tools = [name for name, _ in ranked[:6]]

    # Canonical step sequence: intersect the per-task sequences
    # preserving first-seen order from the most recent task
    canonical_seq: list[str] = []
    for tn in (ordered_tools_by_task[0] if ordered_tools_by_task else []):
        if tn in core_tools and tn not in canonical_seq:
            canonical_seq.append(tn)
    for tn in core_tools:
        if tn not in canonical_seq:
            canonical_seq.append(tn)

    representative = tasks[0]
    rep_id = representative["id"] if hasattr(representative, "keys") else representative[0]
    rep_goal = representative["goal"] if hasattr(representative, "keys") else representative[2]

    definition = {
        "intent": f"{family}_auto",
        "task_type": family,
        "preconditions": [],
        "steps": [{"description": f"call {tn}", "tool": tn} for tn in canonical_seq],
        "tools": core_tools,
        "tool_frequencies": dict(ranked),
        "source": {
            "kind": "capability_version",
            "version_id": version["id"],
            "source_proposal_id": version.get("source_proposal_id"),
            "tasks_sampled": len(tasks),
            "representative_task_id": rep_id,
            "representative_goal": (rep_goal or "")[:200],
        },
        "success_criteria": [f"completed a '{family}' task"],
        "verification_checklist": [f"tools {', '.join(core_tools[:3])} actually invoked"],
        "fallback_hints": [],
    }

    import uuid as _uuid
    sid = f"skill_{_uuid.uuid4().hex[:12]}"
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO skill_registry
               (id, skill_name, intent_family, version, status, definition_json,
                success_rate, usage_count, created_at, updated_at, risk_level,
                source_task_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sid, skill_name, f"{family}_auto", "1.0", "candidate",
                json.dumps(definition, ensure_ascii=False),
                1.0, 0, now, now, "low", rep_id,
            ),
        )

    db._execute_write(_do)
    logger.info(
        "[CapabilityManager] extract_skill: created %s (%s) from version %s "
        "sampling %d tasks",
        sid, skill_name, version["id"], len(tasks),
    )
    return {
        "skill_id": sid,
        "skill_name": skill_name,
        "tasks_sampled": len(tasks),
        "tools": core_tools,
        "canonical_seq": canonical_seq,
        "representative_task_id": rep_id,
    }


def _execute_extract_precedents(
    db: Any, version: dict, action_hint: dict,
) -> dict:
    """Mine the top N recent completed tasks of the target family for
    decision-shaped patterns and seed precedent_records.

    Unlike extract_skill (which produces a reusable tool chain), a
    precedent captures HOW a task was decided — the goal phrasing,
    the criteria satisfied, whether the verifier passed, and how many
    evidence artifacts backed it. Future Planner calls for the same
    family can query find_applicable_precedents to short-circuit to
    known-good patterns.

    Strategy:
      1. Pick up to 10 most recent completed + verified tasks in the family
      2. For each, record a precedent keyed on task_family with decision
         payload summarizing the task, plan, criteria-met count, and
         evidence volume
      3. binding_strength derived from evidence_count (capped at 0.9)
    """
    from brain import precedent_store

    family = action_hint.get("task_family") or version.get("capability_family", "").split(":", 1)[-1]
    limit = int(action_hint.get("limit", 10))

    tasks = db._conn.execute(
        """SELECT id, task_type, goal, risk_level, verification_status,
                  completed_at, plan_json
           FROM tasks
           WHERE status = 'completed' AND task_type = ?
                 AND verification_status = 'pass'
           ORDER BY completed_at DESC LIMIT ?""",
        (family, limit),
    ).fetchall()
    if not tasks:
        return {
            "precedents_created": 0,
            "family": family,
            "tasks_sampled": 0,
            "note": f"no completed+verified tasks for family '{family}' — nothing to extract",
        }

    created: list[str] = []
    for t in tasks:
        td = dict(t) if hasattr(t, "keys") else {}
        tid = td.get("id")
        goal = (td.get("goal") or "")[:250]
        evidence_count = db._conn.execute(
            "SELECT COUNT(*) FROM evidence_records WHERE task_id = ?",
            (tid,),
        ).fetchone()[0]
        criteria_met = db._conn.execute(
            "SELECT COUNT(*) FROM task_criteria WHERE task_id = ? AND status = 'met'",
            (tid,),
        ).fetchone()[0]
        criteria_total = db._conn.execute(
            "SELECT COUNT(*) FROM task_criteria WHERE task_id = ?",
            (tid,),
        ).fetchone()[0]

        decision = {
            "family": family,
            "goal": goal,
            "verification": td.get("verification_status"),
            "criteria_met": criteria_met,
            "criteria_total": criteria_total,
            "evidence_count": evidence_count,
            "source_version_id": version["id"],
            "source_proposal_id": version.get("source_proposal_id"),
        }
        # Stronger precedent when we have more evidence backing the decision
        binding_strength = min(0.5 + evidence_count * 0.01, 0.9)
        pid = precedent_store.create_precedent(
            db,
            precedent_type=f"family_pattern:{family}",
            subject_type="task_family",
            subject_id=family,
            decision=decision,
            binding_strength=binding_strength,
        )
        created.append(pid)

    return {
        "precedents_created": len(created),
        "family": family,
        "tasks_sampled": len(tasks),
        "precedent_ids": created[:3] + (["..."] if len(created) > 3 else []),
    }


def _execute_update_recommended_tools(
    db: Any, version: dict, action_hint: dict,
) -> dict:
    """Materialize a high-performing-tool finding into a routing doctrine.

    action_hint carries:
        {"kind": "update_recommended_tools",
         "task_family": "<family>",
         "prefer": ["tool1", "tool2", ...]}

    We write a doctrine in 'proposed' status so it stays gated behind
    doctrine_engine.ratify_doctrine before any Planner actually reads
    it. This keeps the evolution loop safe — automation writes a
    proposal, a human (or a future governance agent) ratifies it.

    Returns {"doctrine_id", "name", "domain", "prefer": [...]}.
    """
    from brain import doctrine_engine

    family = action_hint.get("task_family") or version.get("capability_family", "").split(":", 1)[-1]
    prefer = action_hint.get("prefer") or []
    if isinstance(prefer, str):
        prefer = [prefer]
    prefer = [str(t).strip() for t in prefer if str(t).strip()]
    if not prefer:
        return {
            "doctrine_id": None,
            "family": family,
            "prefer": [],
            "note": "action_hint.prefer is empty — nothing to route",
        }

    name = f"recommended_tools:{family}"
    definition = {
        "policy": "tool_preference_order",
        "task_family": family,
        "prefer_order": prefer,
        "rationale": (
            f"High-performing tools for '{family}' surfaced by meta_learning; "
            "Planner should try them first before falling back to alternatives."
        ),
        "source": {
            "kind": "capability_version",
            "version_id": version["id"],
            "source_proposal_id": version.get("source_proposal_id"),
        },
    }
    did = doctrine_engine.propose_doctrine(
        db,
        name=name,
        domain="routing",
        definition=definition,
    )
    return {
        "doctrine_id": did,
        "name": name,
        "domain": "routing",
        "status": "proposed",
        "prefer": prefer,
        "note": "doctrine proposed — ratify via doctrine_engine.ratify_doctrine to activate",
    }


def list_versions(
    db: Any,
    *,
    status: Optional[str] = None,
    capability_family: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """Return capability_versions rows, newest first.

    Filters by status (e.g. 'incubating') and/or capability_family when
    given. No filter returns the most recent versions across the board.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if capability_family:
        clauses.append("capability_family = ?")
        params.append(capability_family)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = db._conn.execute(
        f"""SELECT * FROM capability_versions{where}
            ORDER BY created_at DESC LIMIT ?""",
        tuple(params),
    ).fetchall()
    return [dict(r) for r in rows]


def start_experiment(db: Any, version_id: str) -> dict:
    """Transition a capability_version from 'incubating' to 'experimental'.

    Experimental is where the action_hint embedded in the version's
    definition is meant to be actually tried — by operator tooling today,
    by automation later. We bookkeep the transition through the existing
    state machine (no schema change needed); the caller is responsible
    for executing the underlying action or surfacing the action_hint to
    the operator.

    Returns the updated version dict, including its action_hint if the
    source proposal carried one.
    """
    v = get_version(db, version_id)
    if not v:
        raise ValueError(f"Capability version not found: {version_id}")
    if v["status"] != "incubating":
        raise ValueError(
            f"Version {version_id} is in status '{v['status']}' — "
            "only 'incubating' can start an experiment"
        )
    transition_status(
        db, version_id, "experimental",
        reason="operator_started_experiment",
    )
    updated = get_version(db, version_id) or v
    action_hint = {}
    try:
        definition = json.loads(updated.get("definition_json") or "{}")
        source = definition.get("source") or {}
        action_hint = source.get("action_hint") or {}
    except Exception:
        action_hint = {}
    updated["action_hint"] = action_hint
    return updated


def get_capability_stats(db: Any) -> dict:
    """Get aggregate counts of capability versions by status."""
    try:
        rows = db._conn.execute(
            """SELECT status, COUNT(*) as cnt
               FROM capability_versions
               GROUP BY status""",
        ).fetchall()

        counts = {s: 0 for s in CAPABILITY_STATUSES}
        for r in rows:
            counts[r["status"]] = r["cnt"]

        counts["total"] = sum(counts.values())
        return counts

    except Exception as e:
        logger.error("[CapabilityManager] get_capability_stats failed: %s", e)
        return {"total": 0}


# ── Internal ─────────────────────────────────────────────────────


def _next_version(db: Any, capability_family: str) -> int:
    """Determine the next version number for a capability family."""
    row = db._conn.execute(
        """SELECT MAX(version) as max_v FROM capability_versions
           WHERE capability_family = ?""",
        (capability_family,),
    ).fetchone()
    raw = row["max_v"] if row and row["max_v"] is not None else 0
    try:
        current = int(raw)
    except (TypeError, ValueError):
        current = 0
    return current + 1
