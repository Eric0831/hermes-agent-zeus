"""Meta-Learning Layer — system-level optimization from batch task analysis.

Periodically analyzes completed tasks to find system-wide patterns:
- Which planner configurations work best per task family
- Which tools have highest success rates
- Which verification strictness levels are optimal
- Where retry rates are abnormally high

Produces findings that feed into strategy proposals.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _run_id() -> str:
    return f"mlr_{uuid.uuid4().hex[:12]}"


def _finding_id() -> str:
    return f"mlf_{uuid.uuid4().hex[:12]}"


# ── Run Execution ─────────────────────────────────────────────────


def execute_run(
    db: Any,
    *,
    scope_type: str = "global",
    scope_id: Optional[str] = None,
    window_seconds: float = 30 * 86400,  # default 30 days
) -> dict[str, Any]:
    """
    Execute a meta-learning run: analyze recent tasks and produce findings.

    Returns the run summary with findings.
    """
    rid = _run_id()
    now = time.time()
    cutoff = now - window_seconds

    # Create run record
    def _create(conn):
        conn.execute(
            """INSERT INTO meta_learning_runs
               (id, run_type, scope_type, scope_id, status, started_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (rid, "periodic", scope_type, scope_id, "running", now, now),
        )
    db._execute_write(_create)

    try:
        # Gather data
        tasks = _get_tasks_in_window(db, cutoff, scope_id)
        if not tasks:
            _finalize_run(db, rid, 0, 0, {"note": "No tasks in window"})
            return {"run_id": rid, "tasks_analyzed": 0, "findings": []}

        # Analyze
        findings = []
        findings.extend(_analyze_task_families(db, tasks))
        findings.extend(_analyze_tool_performance(db, tasks, cutoff))
        findings.extend(_analyze_verification_patterns(db, tasks))
        findings.extend(_analyze_retry_patterns(db, tasks))

        # Persist findings
        for f in findings:
            fid = _finding_id()
            def _save(conn, _f=f, _fid=fid):
                conn.execute(
                    """INSERT INTO meta_learning_findings
                       (id, run_id, finding_type, task_family, confidence,
                        impact_score, finding_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (_fid, rid, _f["type"], _f.get("task_family"),
                     _f["confidence"], _f["impact"],
                     json.dumps(_f, ensure_ascii=False, default=str), now),
                )
            db._execute_write(_save)
            f["id"] = fid

        _finalize_run(db, rid, len(tasks), len(findings),
                      {"families_analyzed": list({t.get("task_type") for t in tasks})})

        logger.info("[MetaLearn] Run %s: %d tasks → %d findings",
                    rid, len(tasks), len(findings))
        return {"run_id": rid, "tasks_analyzed": len(tasks), "findings": findings}

    except Exception as e:
        logger.error("[MetaLearn] Run %s failed: %s", rid, e)
        _finalize_run(db, rid, 0, 0, {"error": str(e)}, status="failed")
        raise


def get_run(db: Any, run_id: str) -> Optional[dict[str, Any]]:
    row = db._conn.execute(
        "SELECT * FROM meta_learning_runs WHERE id = ?", (run_id,)
    ).fetchone()
    return dict(row) if row else None


def get_findings(db: Any, run_id: str) -> list[dict[str, Any]]:
    rows = db._conn.execute(
        """SELECT * FROM meta_learning_findings WHERE run_id = ?
           ORDER BY impact_score DESC""",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_recent_runs(db: Any, limit: int = 10) -> list[dict[str, Any]]:
    rows = db._conn.execute(
        "SELECT * FROM meta_learning_runs ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Analysis Functions ────────────────────────────────────────────


def _get_tasks_in_window(db, cutoff: float, scope_id: Optional[str]) -> list[dict]:
    conditions = ["created_at >= ?"]
    params: list[Any] = [cutoff]
    if scope_id:
        conditions.append("session_id = ?")
        params.append(scope_id)

    rows = db._conn.execute(
        f"""SELECT id, task_type, goal, status, priority, risk_level,
                   verification_status, retry_count, started_at, completed_at,
                   created_at, failure_reason
            FROM tasks WHERE {' AND '.join(conditions)}
            ORDER BY created_at""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def _analyze_task_families(db, tasks: list[dict]) -> list[dict]:
    """Analyze performance by task family."""
    findings = []
    families: dict[str, list] = {}
    for t in tasks:
        fam = t.get("task_type", "general")
        families.setdefault(fam, []).append(t)

    for fam, fam_tasks in families.items():
        total = len(fam_tasks)
        completed = sum(1 for t in fam_tasks if t["status"] == "completed")
        failed = sum(1 for t in fam_tasks if t["status"] == "failed")
        rate = completed / total if total > 0 else 0

        if total >= 3:
            findings.append({
                "type": "family_performance",
                "task_family": fam,
                "confidence": min(0.5 + total * 0.05, 0.95),
                "impact": abs(rate - 0.7) * total,  # deviation from 70% target
                "detail": {
                    "total": total,
                    "completed": completed,
                    "failed": failed,
                    "completion_rate": rate,
                },
            })

            if rate < 0.5 and total >= 3:
                findings.append({
                    "type": "underperforming_family",
                    "task_family": fam,
                    "confidence": 0.8,
                    "impact": (0.7 - rate) * total * 2,
                    "suggestion": f"Task family '{fam}' has {rate:.0%} completion rate — consider revising planner policy",
                })

    return findings


def _analyze_tool_performance(db, tasks: list[dict], cutoff: float) -> list[dict]:
    """Analyze which tools correlate with success/failure."""
    findings = []

    rows = db._conn.execute(
        """SELECT e.tool_name, t.status, COUNT(*) as cnt
           FROM evidence_records e
           JOIN tasks t ON e.task_id = t.id
           WHERE t.created_at >= ? AND e.tool_name IS NOT NULL
           GROUP BY e.tool_name, t.status""",
        (cutoff,),
    ).fetchall()

    tool_stats: dict[str, dict] = {}
    for r in rows:
        tool = r["tool_name"]
        tool_stats.setdefault(tool, {"completed": 0, "failed": 0, "other": 0})
        if r["status"] == "completed":
            tool_stats[tool]["completed"] += r["cnt"]
        elif r["status"] == "failed":
            tool_stats[tool]["failed"] += r["cnt"]
        else:
            tool_stats[tool]["other"] += r["cnt"]

    for tool, stats in tool_stats.items():
        total = stats["completed"] + stats["failed"]
        if total >= 3:
            rate = stats["completed"] / total
            if rate < 0.5:
                findings.append({
                    "type": "low_tool_success",
                    "task_family": None,
                    "confidence": min(0.6 + total * 0.03, 0.9),
                    "impact": (0.7 - rate) * total,
                    "detail": {"tool": tool, "success_rate": rate, **stats},
                    "suggestion": f"Tool '{tool}' has low success correlation ({rate:.0%})",
                })

    return findings


def _analyze_verification_patterns(db, tasks: list[dict]) -> list[dict]:
    """Analyze verification pass/fail distribution."""
    findings = []
    verified = [t for t in tasks if t.get("verification_status")]

    if len(verified) < 3:
        return findings

    pass_count = sum(1 for t in verified if t["verification_status"] == "pass")
    fail_count = len(verified) - pass_count
    rate = pass_count / len(verified)

    if rate < 0.6:
        findings.append({
            "type": "low_verification_rate",
            "task_family": None,
            "confidence": 0.8,
            "impact": (0.8 - rate) * len(verified),
            "detail": {"pass": pass_count, "fail": fail_count, "rate": rate},
            "suggestion": "Verification pass rate is low — verifier may be too strict or plans too ambitious",
        })

    if rate > 0.95 and len(verified) >= 5:
        findings.append({
            "type": "verification_too_lenient",
            "task_family": None,
            "confidence": 0.6,
            "impact": 0.5,
            "detail": {"pass": pass_count, "fail": fail_count, "rate": rate},
            "suggestion": "Verification pass rate is very high — consider stricter criteria",
        })

    return findings


def _analyze_retry_patterns(db, tasks: list[dict]) -> list[dict]:
    """Analyze retry frequency."""
    findings = []
    with_retries = [t for t in tasks if t.get("retry_count", 0) > 0]

    if len(tasks) >= 5 and len(with_retries) / len(tasks) > 0.3:
        findings.append({
            "type": "high_retry_rate",
            "task_family": None,
            "confidence": 0.75,
            "impact": len(with_retries) * 0.5,
            "detail": {
                "total_tasks": len(tasks),
                "tasks_with_retries": len(with_retries),
                "retry_rate": len(with_retries) / len(tasks),
            },
            "suggestion": "High retry rate — plans may be under-specified or verifier too strict on first pass",
        })

    return findings


# ── Helpers ───────────────────────────────────────────────────────


def _finalize_run(db, rid, tasks_count, findings_count, summary, status="completed"):
    now = time.time()
    def _do(conn):
        conn.execute(
            """UPDATE meta_learning_runs
               SET status = ?, tasks_analyzed = ?, findings_count = ?,
                   summary_json = ?, completed_at = ?
               WHERE id = ?""",
            (status, tasks_count, findings_count,
             json.dumps(summary, ensure_ascii=False, default=str),
             now, rid),
        )
    db._execute_write(_do)
