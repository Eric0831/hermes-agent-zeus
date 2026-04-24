"""Read-only policy boundary status for AgentEOS.

This module reports the current safety boundary without enforcing new
rules. It compares policy evaluations against observed tool evidence so
operators can see where high/medium-risk actions are already audited and
where coverage is missing.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any


BOUNDARY_TARGETS = (
    "agent_admin",
    "send_message",
    "terminal",
    "shell_exec_sandboxed",
    "patch",
    "write_file",
    "process",
    "cronjob",
    "execute_code",
    "delegate_task",
    "mcp_git_git_push",
    "mcp_git_git_reset",
)

RISKY_LEVELS = {"medium", "high"}
CAPABILITY_REVIEW_THRESHOLD = 0.30


def collect_status(db: Any, *, limit: int = 8) -> dict[str, Any]:
    """Collect a compact read-only snapshot of policy boundary health."""
    if db is None:
        return {"present": False, "reason": "no_db"}

    profiles = _risk_profiles()
    tool_counts = _observed_tool_counts(db)
    eval_target_counts = _policy_target_counts(db)
    risky_evidence = _risky_evidence_stats(db, profiles, limit=limit)

    return {
        "present": True,
        "risk_profile_counts": _profile_counts(profiles),
        "boundary_targets": _boundary_target_stats(
            profiles,
            tool_counts.get("evidence", {}),
            tool_counts.get("messages", {}),
            eval_target_counts,
        ),
        "policy_evaluations": _policy_evaluation_stats(db, limit=limit),
        "risky_tool_evidence": risky_evidence,
        "unmapped_tools": _unmapped_tools(profiles, tool_counts, limit=limit),
        "open_approval_tasks": _open_approval_tasks(db, limit=limit),
        "open_risky_versions": _open_risky_versions(db, limit=limit),
    }


def format_status(snapshot: dict[str, Any]) -> str:
    """Format policy status as compact Markdown."""
    if not snapshot.get("present"):
        return f"Policy status unavailable: {snapshot.get('reason') or 'unknown'}"

    profile_counts = snapshot.get("risk_profile_counts", {})
    evaluations = snapshot.get("policy_evaluations", {})
    decisions = evaluations.get("decision_counts", {})
    risks = evaluations.get("risk_counts", {})
    evidence = snapshot.get("risky_tool_evidence", {})

    lines = [
        "**Policy Boundary Status**",
        "",
        "Risk profile registry:",
        (
            f"  high={profile_counts.get('high', 0)} | "
            f"medium={profile_counts.get('medium', 0)} | "
            f"low={profile_counts.get('low', 0)}"
        ),
        "Policy evaluations:",
        (
            f"  total={evaluations.get('total', 0)} | "
            f"allow={decisions.get('allow', 0)} | "
            f"allow_with_approval={decisions.get('allow_with_approval', 0)} | "
            f"deny={decisions.get('deny', 0)}"
        ),
        (
            f"  risk: low={risks.get('low', 0)} | "
            f"medium={risks.get('medium', 0)} | "
            f"high={risks.get('high', 0)}"
        ),
        "Risky tool evidence:",
        (
            f"  total={evidence.get('total', 0)} | "
            f"covered={evidence.get('covered', 0)} | "
            f"uncovered={evidence.get('uncovered', 0)} | "
            f"coverage={evidence.get('coverage_pct', 0.0):.1f}%"
        ),
        (
            f"  high={evidence.get('risk_counts', {}).get('high', 0)} | "
            f"medium={evidence.get('risk_counts', {}).get('medium', 0)}"
        ),
        "  coverage means same task_id + tool target has a policy_evaluation",
        "",
        "Boundary targets:",
    ]

    targets = snapshot.get("boundary_targets", [])
    if not targets:
        lines.append("  (none configured)")
    for item in targets:
        if item.get("risk") == "low":
            continue
        lines.append(
            f"  {item['target']:24s} risk={item['risk']:6s} "
            f"evidence={item['evidence_count']} "
            f"messages={item['message_tool_count']} "
            f"policy={item['policy_eval_count']}"
        )

    open_tasks = snapshot.get("open_approval_tasks", {})
    lines.append("")
    lines.append(f"Open approval tasks ({len(open_tasks.get('samples', []))} shown / {open_tasks.get('total', 0)} total):")
    if not open_tasks.get("samples"):
        lines.append("  (none)")
    for task in open_tasks.get("samples", []):
        lines.append(
            f"  `{task['id']}` [{task['status']}] risk={task['risk_level']} {task['task_type']}"
        )
        if task.get("goal"):
            lines.append(f"    {task['goal']}")

    versions = snapshot.get("open_risky_versions", {})
    lines.append("")
    lines.append(
        f"Open risky capability versions ({len(versions.get('samples', []))} shown / {versions.get('total', 0)} total):"
    )
    if not versions.get("samples"):
        lines.append("  (none)")
    for version in versions.get("samples", []):
        lines.append(
            f"  `{version['id']}` [{version['status']}] "
            f"risk={version['risk_score']:.2f} {version['family']}"
        )
        if version.get("title"):
            lines.append(f"    {version['title']}")

    uncovered = evidence.get("uncovered_samples", [])
    lines.append("")
    lines.append(f"Uncovered risky tool evidence ({len(uncovered)} shown):")
    if not uncovered:
        lines.append("  (none)")
    for item in uncovered:
        lines.append(
            f"  {item['when']} `{item['task_id']}` {item['tool_name']} "
            f"risk={item['tool_risk']} task={item['task_risk']}"
        )
        if item.get("summary"):
            lines.append(f"    {item['summary']}")

    unmapped = snapshot.get("unmapped_tools", {})
    if unmapped.get("total"):
        lines.append("")
        lines.append(f"Unmapped observed tools ({unmapped['total']} tool names):")
        for item in unmapped.get("samples", []):
            lines.append(
                f"  {item['tool_name']}: evidence={item['evidence_count']} "
                f"messages={item['message_tool_count']}"
            )

    recent = evaluations.get("recent", [])
    lines.append("")
    lines.append(f"Recent policy decisions ({len(recent)} shown):")
    if not recent:
        lines.append("  (none)")
    for item in recent:
        lines.append(
            f"  {item['when']} {item['decision']:19s} risk={item['risk_level']:6s} "
            f"{item['action_type']}:{item['target']}"
        )
        if item.get("reason"):
            lines.append(f"    {item['reason']}")

    lines.extend([
        "",
        "Read-only report. No policy enforcement changes were applied.",
    ])
    return "\n".join(lines)


def _risk_profiles() -> dict[str, str]:
    try:
        from brain.policy import _TOOL_RISK_PROFILES
        return dict(_TOOL_RISK_PROFILES)
    except Exception:
        return {}


def _profile_counts(profiles: dict[str, str]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for risk in profiles.values():
        counts[str(risk or "unknown")] += 1
    return {k: int(v) for k, v in counts.items()}


def _policy_evaluation_stats(db: Any, *, limit: int) -> dict[str, Any]:
    if not _table_exists(db, "policy_evaluations"):
        return {
            "total": 0,
            "decision_counts": {},
            "risk_counts": {},
            "action_counts": {},
            "recent": [],
        }

    decision_counts = _count_by(db, "policy_evaluations", "decision")
    risk_counts = _count_by(db, "policy_evaluations", "risk_level")
    action_counts = _count_by(db, "policy_evaluations", "action_type")
    total = sum(decision_counts.values())

    try:
        rows = db._conn.execute(
            """SELECT task_id, action_type, target, risk_level, decision, reason, created_at
               FROM policy_evaluations
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    except Exception:
        rows = []

    return {
        "total": total,
        "decision_counts": decision_counts,
        "risk_counts": risk_counts,
        "action_counts": action_counts,
        "recent": [
            {
                "task_id": r["task_id"] or "-",
                "action_type": r["action_type"] or "-",
                "target": r["target"] or "-",
                "risk_level": r["risk_level"] or "unknown",
                "decision": r["decision"] or "-",
                "reason": _compact(r["reason"] or "", 120),
                "when": _fmt_time(r["created_at"]),
            }
            for r in rows
        ],
    }


def _risky_evidence_stats(
    db: Any,
    profiles: dict[str, str],
    *,
    limit: int,
) -> dict[str, Any]:
    if not _table_exists(db, "evidence_records"):
        return _empty_risky_evidence()

    covered_pairs = _covered_task_tool_pairs(db)
    try:
        rows = db._conn.execute(
            """SELECT e.id, e.task_id, e.tool_name, e.summary, e.created_at,
                      t.task_type, t.risk_level AS task_risk, t.status
               FROM evidence_records e
               LEFT JOIN tasks t ON t.id = e.task_id
               WHERE e.tool_name IS NOT NULL
               ORDER BY e.created_at DESC"""
        ).fetchall()
    except Exception:
        return _empty_risky_evidence()

    total = 0
    uncovered = 0
    risk_counts: Counter[str] = Counter()
    tool_counts: Counter[str] = Counter()
    uncovered_samples: list[dict[str, Any]] = []

    for raw in rows:
        row = dict(raw)
        tool = str(row.get("tool_name") or "")
        risk = profiles.get(tool, "unknown")
        if risk not in RISKY_LEVELS:
            continue

        total += 1
        risk_counts[risk] += 1
        tool_counts[tool] += 1
        pair = (str(row.get("task_id") or ""), tool)
        if pair in covered_pairs:
            continue

        uncovered += 1
        if len(uncovered_samples) < limit:
            uncovered_samples.append({
                "id": row.get("id") or "-",
                "task_id": row.get("task_id") or "-",
                "tool_name": tool,
                "tool_risk": risk,
                "task_type": row.get("task_type") or "-",
                "task_risk": row.get("task_risk") or "unknown",
                "status": row.get("status") or "-",
                "summary": _compact(row.get("summary") or "", 120),
                "when": _fmt_time(row.get("created_at")),
            })

    covered = total - uncovered
    return {
        "total": total,
        "covered": covered,
        "uncovered": uncovered,
        "coverage_pct": round(100.0 * covered / total, 1) if total else 100.0,
        "risk_counts": dict(risk_counts),
        "tool_counts": dict(tool_counts.most_common(12)),
        "uncovered_samples": uncovered_samples,
    }


def _boundary_target_stats(
    profiles: dict[str, str],
    evidence_counts: dict[str, int],
    message_counts: dict[str, int],
    policy_counts: dict[str, int],
) -> list[dict[str, Any]]:
    targets = []
    for target in BOUNDARY_TARGETS:
        targets.append({
            "target": target,
            "risk": profiles.get(target, "unknown"),
            "evidence_count": int(evidence_counts.get(target, 0)),
            "message_tool_count": int(message_counts.get(target, 0)),
            "policy_eval_count": int(policy_counts.get(target, 0)),
        })
    return targets


def _open_approval_tasks(db: Any, *, limit: int) -> dict[str, Any]:
    if not _table_exists(db, "tasks"):
        return {"total": 0, "samples": []}
    try:
        row = db._conn.execute(
            """SELECT COUNT(*) AS cnt
               FROM tasks
               WHERE requires_approval = 1
                 AND status IN ('received', 'triaged', 'planned', 'running', 'verifying', 'blocked')"""
        ).fetchone()
        total = int(row["cnt"] or 0) if row else 0
        rows = db._conn.execute(
            """SELECT id, task_type, goal, status, risk_level, created_at
               FROM tasks
               WHERE requires_approval = 1
                 AND status IN ('received', 'triaged', 'planned', 'running', 'verifying', 'blocked')
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    except Exception:
        return {"total": 0, "samples": []}

    return {
        "total": total,
        "samples": [
            {
                "id": r["id"],
                "task_type": r["task_type"] or "-",
                "goal": _compact(r["goal"] or "", 120),
                "status": r["status"] or "-",
                "risk_level": r["risk_level"] or "unknown",
                "when": _fmt_time(r["created_at"]),
            }
            for r in rows
        ],
    }


def _open_risky_versions(db: Any, *, limit: int) -> dict[str, Any]:
    if not _table_exists(db, "capability_versions"):
        return {"total": 0, "samples": []}
    try:
        rows = db._conn.execute(
            """SELECT v.id, v.capability_family, v.status, v.definition_json,
                      p.title, p.risk_score, v.created_at
               FROM capability_versions v
               LEFT JOIN capability_proposals p ON p.id = v.source_proposal_id
               WHERE v.status IN ('incubating', 'experimental', 'limited_rollout')
                 AND COALESCE(p.risk_score, 0.0) >= ?
               ORDER BY p.risk_score DESC, v.created_at DESC""",
            (CAPABILITY_REVIEW_THRESHOLD,),
        ).fetchall()
    except Exception:
        return {"total": 0, "samples": []}

    samples = []
    for raw in rows[:limit]:
        row = dict(raw)
        samples.append({
            "id": row.get("id") or "-",
            "family": row.get("capability_family") or "-",
            "status": row.get("status") or "-",
            "title": _compact(row.get("title") or "", 100),
            "risk_score": float(row.get("risk_score") or 0.0),
            "action_kind": _action_hint(row.get("definition_json")).get("kind", ""),
            "when": _fmt_time(row.get("created_at")),
        })

    return {"total": len(rows), "samples": samples}


def _observed_tool_counts(db: Any) -> dict[str, dict[str, int]]:
    return {
        "evidence": _tool_counts(db, "evidence_records", "tool_name"),
        "messages": _tool_counts(db, "messages", "tool_name"),
    }


def _unmapped_tools(
    profiles: dict[str, str],
    tool_counts: dict[str, dict[str, int]],
    *,
    limit: int,
) -> dict[str, Any]:
    evidence_counts = tool_counts.get("evidence", {})
    message_counts = tool_counts.get("messages", {})
    names = sorted((set(evidence_counts) | set(message_counts)) - set(profiles))
    samples = [
        {
            "tool_name": name,
            "evidence_count": int(evidence_counts.get(name, 0)),
            "message_tool_count": int(message_counts.get(name, 0)),
        }
        for name in names[:limit]
    ]
    return {"total": len(names), "samples": samples}


def _covered_task_tool_pairs(db: Any) -> set[tuple[str, str]]:
    if not _table_exists(db, "policy_evaluations"):
        return set()
    try:
        rows = db._conn.execute(
            """SELECT task_id, target
               FROM policy_evaluations
               WHERE task_id IS NOT NULL AND target IS NOT NULL"""
        ).fetchall()
    except Exception:
        return set()
    return {
        (str(r["task_id"] or ""), str(r["target"] or ""))
        for r in rows
        if r["task_id"] and r["target"]
    }


def _policy_target_counts(db: Any) -> dict[str, int]:
    if not _table_exists(db, "policy_evaluations"):
        return {}
    try:
        rows = db._conn.execute(
            """SELECT target, COUNT(*) AS cnt
               FROM policy_evaluations
               WHERE target IS NOT NULL
               GROUP BY target"""
        ).fetchall()
    except Exception:
        return {}
    return {str(r["target"]): int(r["cnt"] or 0) for r in rows}


def _tool_counts(db: Any, table: str, column: str) -> dict[str, int]:
    if not _table_exists(db, table):
        return {}
    try:
        rows = db._conn.execute(
            f"""SELECT {column} AS tool_name, COUNT(*) AS cnt
                FROM {table}
                WHERE {column} IS NOT NULL AND {column} != ''
                GROUP BY {column}"""
        ).fetchall()
    except Exception:
        return {}
    return {str(r["tool_name"]): int(r["cnt"] or 0) for r in rows}


def _count_by(db: Any, table: str, column: str) -> dict[str, int]:
    if not _table_exists(db, table):
        return {}
    try:
        rows = db._conn.execute(
            f"SELECT {column} AS key, COUNT(*) AS cnt FROM {table} GROUP BY {column}"
        ).fetchall()
    except Exception:
        return {}
    return {str(r["key"] or "unknown"): int(r["cnt"] or 0) for r in rows}


def _table_exists(db: Any, table: str) -> bool:
    try:
        row = db._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _action_hint(definition_json: str | None) -> dict[str, Any]:
    try:
        definition = json.loads(definition_json or "{}")
    except Exception:
        definition = {}
    source = definition.get("source") or {}
    hint = source.get("action_hint") or {}
    return hint if isinstance(hint, dict) else {}


def _empty_risky_evidence() -> dict[str, Any]:
    return {
        "total": 0,
        "covered": 0,
        "uncovered": 0,
        "coverage_pct": 100.0,
        "risk_counts": {},
        "tool_counts": {},
        "uncovered_samples": [],
    }


def _fmt_time(ts: Any) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts or 0)))
    except Exception:
        return "-- --:--"


def _compact(text: str, limit: int) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)] + "..."
