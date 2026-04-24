"""Closed-loop status reporting for AgentEOS.

Read-only helpers used by gateway commands and future dashboards. This
module never advances lifecycle state and never executes action_hints.
"""
from __future__ import annotations

import json
import time
from typing import Any


WATCH_STATUSES = ("incubating", "experimental", "limited_rollout", "adopted")


def collect_status(db: Any, *, limit: int = 8) -> dict[str, Any]:
    """Collect a compact read-only snapshot of the closed-loop controller."""
    if db is None:
        return {"present": False, "reason": "no_db"}

    return {
        "present": True,
        "capability_counts": _counts(
            db,
            "capability_versions",
            "status",
            default_keys=[
                "proposed", "incubating", "experimental",
                "limited_rollout", "adopted", "deprecated", "retired",
            ],
        ),
        "skill_candidates": _skill_candidate_counts(db),
        "open_versions": _open_versions(db, limit=limit),
        "recent_runs": _recent_runs(db, limit=min(limit, 6)),
    }


def format_status(snapshot: dict[str, Any]) -> str:
    """Format closed-loop status as compact Markdown."""
    if not snapshot.get("present"):
        return "Closed-loop status unavailable: no SessionDB."

    counts = snapshot.get("capability_counts", {})
    skill_counts = snapshot.get("skill_candidates", {})
    lines = [
        "**Closed Loop Status**",
        "",
        "Capability lifecycle:",
        (
            f"  incubating={counts.get('incubating', 0)} | "
            f"experimental={counts.get('experimental', 0)} | "
            f"limited_rollout={counts.get('limited_rollout', 0)} | "
            f"adopted={counts.get('adopted', 0)}"
        ),
        "Skill candidates:",
        (
            f"  low={skill_counts.get('low', 0)} | "
            f"medium={skill_counts.get('medium', 0)} | "
            f"high={skill_counts.get('high', 0)} | "
            f"unknown={skill_counts.get('unknown', 0)}"
        ),
        "",
    ]

    open_versions = snapshot.get("open_versions", [])
    lines.append(f"Open capability versions ({len(open_versions)} shown):")
    if not open_versions:
        lines.append("  (none)")
    for item in open_versions:
        lines.append(
            f"  `{item['id']}` [{item['status']}] {item['family']} "
            f"kind={item.get('action_kind') or '-'} "
            f"gain={item.get('expected_gain', 0.0):.2f} "
            f"risk={item.get('risk_score', 0.0):.2f}"
        )
        if item.get("title"):
            lines.append(f"    {item['title'][:100]}")

    runs = snapshot.get("recent_runs", [])
    lines.append("")
    lines.append(f"Recent controller runs ({len(runs)} shown):")
    if not runs:
        lines.append("  (no closed_loop_runs logged)")
    for r in runs:
        lines.append(
            f"  {r['when']} {r['gateway']} `{r['version_id']}` "
            f"{r['before_status']}->{r['after_status']} "
            f"{r['action_kind'] or '-'} decision={r['decision']}"
        )
        if r.get("reason"):
            lines.append(f"    {r['reason'][:100]}")

    lines.extend([
        "",
        "Controller does not auto-adopt. `limited_rollout` still needs validation before adoption.",
    ])
    return "\n".join(lines)


def _counts(
    db: Any,
    table: str,
    column: str,
    *,
    default_keys: list[str] | None = None,
) -> dict[str, int]:
    counts = {k: 0 for k in (default_keys or [])}
    try:
        rows = db._conn.execute(
            f"SELECT {column}, COUNT(*) AS cnt FROM {table} GROUP BY {column}"
        ).fetchall()
        for r in rows:
            key = r[column] if hasattr(r, "keys") else r[0]
            val = r["cnt"] if hasattr(r, "keys") else r[1]
            counts[str(key or "unknown")] = int(val or 0)
    except Exception:
        pass
    return counts


def _skill_candidate_counts(db: Any) -> dict[str, int]:
    counts = {"low": 0, "medium": 0, "high": 0, "unknown": 0}
    try:
        rows = db._conn.execute(
            """SELECT COALESCE(risk_level, 'unknown') AS risk_level,
                      COUNT(*) AS cnt
               FROM skill_registry
               WHERE status = 'candidate'
               GROUP BY COALESCE(risk_level, 'unknown')"""
        ).fetchall()
        for r in rows:
            risk = r["risk_level"] if hasattr(r, "keys") else r[0]
            cnt = r["cnt"] if hasattr(r, "keys") else r[1]
            counts[str(risk or "unknown")] = int(cnt or 0)
    except Exception:
        pass
    return counts


def _open_versions(db: Any, *, limit: int) -> list[dict[str, Any]]:
    try:
        rows = db._conn.execute(
            """SELECT v.id, v.capability_family, v.version, v.status,
                      v.source_proposal_id, v.definition_json, v.created_at,
                      p.title, p.expected_gain, p.risk_score
               FROM capability_versions v
               LEFT JOIN capability_proposals p ON p.id = v.source_proposal_id
               WHERE v.status IN ('incubating', 'experimental', 'limited_rollout', 'adopted')
               ORDER BY
                 CASE v.status
                   WHEN 'limited_rollout' THEN 0
                   WHEN 'experimental' THEN 1
                   WHEN 'incubating' THEN 2
                   WHEN 'adopted' THEN 3
                   ELSE 4
                 END,
                 v.created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    except Exception:
        return []

    versions = []
    for r in rows:
        row = dict(r)
        action_hint = _action_hint(row.get("definition_json"))
        versions.append({
            "id": row["id"],
            "family": row["capability_family"],
            "version": row["version"],
            "status": row["status"],
            "source_proposal_id": row.get("source_proposal_id"),
            "action_kind": action_hint.get("kind", ""),
            "title": row.get("title") or "",
            "expected_gain": float(row.get("expected_gain") or 0.0),
            "risk_score": float(row.get("risk_score") or 0.0),
        })
    return versions


def _recent_runs(db: Any, *, limit: int) -> list[dict[str, Any]]:
    if not _table_exists(db, "closed_loop_runs"):
        return []
    try:
        rows = db._conn.execute(
            """SELECT gateway, version_id, action_kind, before_status,
                      after_status, decision, reason, created_at
               FROM closed_loop_runs
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    except Exception:
        return []

    runs = []
    for r in rows:
        row = dict(r)
        runs.append({
            "gateway": row.get("gateway") or "-",
            "version_id": row.get("version_id") or "-",
            "action_kind": row.get("action_kind") or "",
            "before_status": row.get("before_status") or "-",
            "after_status": row.get("after_status") or "-",
            "decision": row.get("decision") or "-",
            "reason": row.get("reason") or "",
            "when": _fmt_time(row.get("created_at")),
        })
    return runs


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


def _fmt_time(ts: Any) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts or 0)))
    except Exception:
        return "-- --:--"
