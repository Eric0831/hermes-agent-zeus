"""Pattern Mining — cross-task pattern extraction and persistence.

Analyzes completed tasks within a task family to extract reusable patterns:
- success_archetype: common tool chains and step sequences in successful tasks
- failure_archetype: common failure modes and root causes
- risk_precursor: patterns that precede failures (early warning signals)
- tool_chain_pattern: frequently co-occurring tools

Patterns are persisted with confidence scores and support counts,
enabling the strategy layer to make data-driven policy adjustments.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import Counter
from typing import Any, Optional

logger = logging.getLogger(__name__)

PATTERN_TYPES = (
    "success_archetype",
    "failure_archetype",
    "risk_precursor",
    "tool_chain_pattern",
)


def _pattern_id() -> str:
    return f"pat_{uuid.uuid4().hex[:12]}"


# -- Mining Engine --------------------------------------------------------


def mine_patterns(
    db: Any,
    task_family: str,
    *,
    min_support: int = 3,
) -> list[dict]:
    """Analyze completed tasks in a family and extract cross-task patterns.

    Only considers tasks that reached a terminal state (completed or failed).
    Requires at least ``min_support`` tasks to produce a pattern.

    Returns a list of pattern dicts ready for ``save_pattern()``.
    """
    try:
        tasks = _get_family_tasks(db, task_family)
        if len(tasks) < min_support:
            logger.debug(
                "[PatternMine] Not enough tasks for family '%s' (%d < %d)",
                task_family, len(tasks), min_support,
            )
            return []

        # Prefetch all tool chains in one batch to avoid N+1 queries
        tools_cache = _prefetch_task_tools(db, [t["id"] for t in tasks])

        patterns: list[dict] = []
        patterns.extend(_extract_success_archetypes(tasks, task_family, min_support, tools_cache))
        patterns.extend(_extract_failure_archetypes(tasks, task_family, min_support))
        patterns.extend(_extract_risk_precursors(tasks, task_family, min_support, tools_cache))
        patterns.extend(_extract_tool_chain_patterns(tasks, task_family, min_support, tools_cache))

        logger.info(
            "[PatternMine] Family '%s': %d tasks -> %d patterns",
            task_family, len(tasks), len(patterns),
        )
        return patterns

    except Exception as e:
        logger.error("[PatternMine] Mining failed for '%s': %s", task_family, e)
        return []


# -- Persistence -----------------------------------------------------------


def save_pattern(
    db: Any,
    pattern_type: str,
    task_family: str,
    pattern_data: dict,
    confidence: float,
    support_count: int,
) -> str:
    """Persist a mined pattern to the cross_task_patterns table.

    Returns the new pattern id.
    """
    pid = _pattern_id()
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO cross_task_patterns
               (id, pattern_type, task_family, pattern_json, confidence,
                support_count, success_delta, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pid, pattern_type, task_family,
                json.dumps(pattern_data, ensure_ascii=False, default=str),
                confidence, support_count,
                pattern_data.get("success_delta", 0.0),
                "active", now, now,
            ),
        )

    db._execute_write(_do)
    logger.info(
        "[PatternMine] Saved pattern %s [%s] family='%s' confidence=%.2f",
        pid, pattern_type, task_family, confidence,
    )
    return pid


def get_patterns(
    db: Any,
    task_family: str,
    *,
    status: str = "active",
) -> list[dict]:
    """Retrieve patterns for a task family filtered by status."""
    try:
        rows = db._conn.execute(
            """SELECT * FROM cross_task_patterns
               WHERE task_family = ? AND status = ?
               ORDER BY confidence DESC, support_count DESC""",
            (task_family, status),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[PatternMine] get_patterns failed: %s", e)
        return []


def get_best_pattern(db: Any, task_family: str) -> Optional[dict]:
    """Return the highest-confidence active pattern for a task family."""
    try:
        row = db._conn.execute(
            """SELECT * FROM cross_task_patterns
               WHERE task_family = ? AND status = 'active'
               ORDER BY confidence DESC, support_count DESC
               LIMIT 1""",
            (task_family,),
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("[PatternMine] get_best_pattern failed: %s", e)
        return None


# -- Internal Extraction Functions -----------------------------------------


def _get_family_tasks(db: Any, task_family: str) -> list[dict]:
    """Fetch terminal-state tasks for a given family."""
    rows = db._conn.execute(
        """SELECT id, task_type, goal, status, priority, risk_level,
                  verification_status, retry_count, failure_reason,
                  plan_json, started_at, completed_at, created_at
           FROM tasks
           WHERE task_type = ? AND status IN ('completed', 'failed')
           ORDER BY created_at""",
        (task_family,),
    ).fetchall()
    return [dict(r) for r in rows]


def _prefetch_task_tools(db: Any, task_ids: list[str]) -> dict[str, list[str]]:
    """Batch-fetch tool chains for all tasks in one query (avoids N+1)."""
    if not task_ids:
        return {}
    placeholders = ",".join("?" for _ in task_ids)
    rows = db._conn.execute(
        f"""SELECT task_id, tool_name FROM evidence_records
            WHERE task_id IN ({placeholders}) AND tool_name IS NOT NULL
            ORDER BY task_id, created_at""",
        task_ids,
    ).fetchall()
    cache: dict[str, list[str]] = {tid: [] for tid in task_ids}
    for r in rows:
        cache[r["task_id"]].append(r["tool_name"])
    return cache


def _get_task_tools(db: Any, task_id: str) -> list[str]:
    """Get ordered list of tools used in a task via evidence records."""
    rows = db._conn.execute(
        """SELECT tool_name FROM evidence_records
           WHERE task_id = ? AND tool_name IS NOT NULL
           ORDER BY created_at""",
        (task_id,),
    ).fetchall()
    return [r["tool_name"] for r in rows]


def _extract_success_archetypes(
    tasks: list[dict], task_family: str, min_support: int,
    tools_cache: dict[str, list[str]],
) -> list[dict]:
    """Find common tool chains in successful tasks."""
    patterns = []
    successful = [t for t in tasks if t["status"] == "completed"]
    if len(successful) < min_support:
        return patterns

    # Collect tool chains for each successful task (from cache)
    tool_chains: list[tuple[str, ...]] = []
    for t in successful:
        chain = tuple(tools_cache.get(t["id"], []))
        if chain:
            tool_chains.append(chain)

    if len(tool_chains) < min_support:
        return patterns

    # Build lookup: task_id -> chain tuple (from cache, no re-query)
    task_chain_map = {t["id"]: tuple(tools_cache.get(t["id"], [])) for t in successful}

    # Find the most common tool chain
    chain_counter = Counter(tool_chains)
    for chain, count in chain_counter.most_common(3):
        if count >= min_support:
            # Calculate average duration for tasks using this chain
            durations = []
            for t in successful:
                if task_chain_map[t["id"]] == chain and t.get("started_at") and t.get("completed_at"):
                    durations.append(t["completed_at"] - t["started_at"])

            avg_duration = sum(durations) / len(durations) if durations else None

            patterns.append({
                "pattern_type": "success_archetype",
                "task_family": task_family,
                "pattern_data": {
                    "tool_chain": list(chain),
                    "avg_duration_s": avg_duration,
                    "sample_count": count,
                },
                "confidence": min(0.5 + count * 0.1, 0.95),
                "support_count": count,
            })

    return patterns


def _extract_failure_archetypes(
    tasks: list[dict], task_family: str, min_support: int,
) -> list[dict]:
    """Find common failure modes."""
    patterns = []
    failed = [t for t in tasks if t["status"] == "failed"]
    if len(failed) < min_support:
        return patterns

    # Group by failure reason
    reason_counter: Counter[str] = Counter()
    for t in failed:
        reason = t.get("failure_reason") or "unknown"
        # Normalize by taking the first line / first 80 chars
        normalized = reason.strip().split("\n")[0][:80]
        reason_counter[normalized] += 1

    total_failed = len(failed)
    for reason, count in reason_counter.most_common(3):
        if count >= min_support:
            patterns.append({
                "pattern_type": "failure_archetype",
                "task_family": task_family,
                "pattern_data": {
                    "failure_reason": reason,
                    "occurrence_count": count,
                    "failure_share": count / total_failed if total_failed else 0,
                },
                "confidence": min(0.5 + count * 0.1, 0.95),
                "support_count": count,
            })

    return patterns


def _extract_risk_precursors(
    tasks: list[dict], task_family: str, min_support: int,
    tools_cache: dict[str, list[str]],
) -> list[dict]:
    """Find patterns that precede failures (high retry count, specific tools)."""
    patterns = []
    failed = [t for t in tasks if t["status"] == "failed"]
    successful = [t for t in tasks if t["status"] == "completed"]

    if len(failed) < min_support or not successful:
        return patterns

    # Check if high retry count is a precursor to failure
    failed_retry_avg = (
        sum(t.get("retry_count", 0) for t in failed) / len(failed)
    )
    success_retry_avg = (
        sum(t.get("retry_count", 0) for t in successful) / len(successful)
    )

    if failed_retry_avg > success_retry_avg * 1.5 and len(failed) >= min_support:
        patterns.append({
            "pattern_type": "risk_precursor",
            "task_family": task_family,
            "pattern_data": {
                "precursor": "high_retry_count",
                "failed_avg_retries": failed_retry_avg,
                "success_avg_retries": success_retry_avg,
                "success_delta": -(failed_retry_avg - success_retry_avg),
            },
            "confidence": 0.7,
            "support_count": len(failed),
        })

    # Check for tools that appear disproportionately in failed tasks
    failed_tools: Counter[str] = Counter()
    success_tools: Counter[str] = Counter()
    for t in failed:
        for tool in set(tools_cache.get(t["id"], [])):
            failed_tools[tool] += 1
    for t in successful:
        for tool in set(tools_cache.get(t["id"], [])):
            success_tools[tool] += 1

    for tool, f_count in failed_tools.items():
        s_count = success_tools.get(tool, 0)
        f_rate = f_count / len(failed)
        s_rate = s_count / len(successful) if successful else 0
        if f_rate > s_rate * 2 and f_count >= min_support:
            patterns.append({
                "pattern_type": "risk_precursor",
                "task_family": task_family,
                "pattern_data": {
                    "precursor": "risky_tool",
                    "tool": tool,
                    "failure_rate": f_rate,
                    "success_rate": s_rate,
                    "success_delta": -(f_rate - s_rate),
                },
                "confidence": min(0.5 + f_count * 0.08, 0.9),
                "support_count": f_count,
            })

    return patterns


def _extract_tool_chain_patterns(
    tasks: list[dict], task_family: str, min_support: int,
    tools_cache: dict[str, list[str]],
) -> list[dict]:
    """Find frequently co-occurring tools (bigrams)."""
    patterns = []
    bigram_counter: Counter[tuple[str, str]] = Counter()

    # Build per-task chain from cache
    task_chains = {t["id"]: tools_cache.get(t["id"], []) for t in tasks}

    for tid, tools in task_chains.items():
        for i in range(len(tools) - 1):
            bigram_counter[(tools[i], tools[i + 1])] += 1

    total_tasks = len(tasks)
    for bigram, count in bigram_counter.most_common(5):
        if count >= min_support:
            # Calculate success rate for tasks containing this bigram
            tasks_with_bigram = 0
            successes_with_bigram = 0
            for t in tasks:
                chain = task_chains[t["id"]]
                for i in range(len(chain) - 1):
                    if (chain[i], chain[i + 1]) == bigram:
                        tasks_with_bigram += 1
                        if t["status"] == "completed":
                            successes_with_bigram += 1
                        break

            success_rate = (
                successes_with_bigram / tasks_with_bigram
                if tasks_with_bigram > 0 else 0
            )

            patterns.append({
                "pattern_type": "tool_chain_pattern",
                "task_family": task_family,
                "pattern_data": {
                    "tool_a": bigram[0],
                    "tool_b": bigram[1],
                    "co_occurrence_count": count,
                    "co_occurrence_rate": count / total_tasks if total_tasks else 0,
                    "success_rate_with_chain": success_rate,
                    "success_delta": success_rate - 0.7,  # delta vs 70% baseline
                },
                "confidence": min(0.4 + count * 0.1, 0.9),
                "support_count": count,
            })

    return patterns
