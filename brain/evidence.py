"""Evidence Store — capture and query task completion evidence.

Every tool call result and final LLM response during a task is captured
as an evidence record. The Verifier uses these to check whether success
criteria are actually met with proof, not just the model's claim.

NOTE: SessionDB._execute_write() expects a callable fn(conn) — NOT raw SQL.
Reads can use db._conn directly (WAL allows concurrent readers).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _eid() -> str:
    return f"ev_{uuid.uuid4().hex[:12]}"


# ── Capture ───────────────────────────────────────────────────────


def capture_from_tool_result(
    task_id: str,
    tool_name: str,
    tool_call_id: str,
    output: str,
    db: Any,
) -> str:
    """
    Capture evidence from a tool call result.

    Args:
        task_id: The task this evidence belongs to
        tool_name: Name of the tool that produced the output
        tool_call_id: Unique ID of the tool call
        output: Raw tool output string
        db: SessionDB instance

    Returns:
        evidence_id
    """
    summary = _extract_summary(output, tool_name)
    payload = _truncate_payload(output)
    eid = _eid()
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO evidence_records
               (id, task_id, source_type, source_ref, tool_name,
                summary, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (eid, task_id, "tool_output", tool_call_id, tool_name,
             summary, payload, now),
        )

    db._execute_write(_do)
    logger.debug("Evidence %s captured from %s for task %s", eid, tool_name, task_id)
    return eid


def capture_from_response(
    task_id: str,
    response_text: str,
    db: Any,
) -> str:
    """Capture the final LLM response as evidence.
    
    For substantial responses (>200 chars), captures structured summary
    including response length and key content markers — critical for
    exploratory/research tasks where the response IS the deliverable.
    """
    eid = _eid()
    if not response_text:
        summary = "(empty response)"
    elif len(response_text) > 200:
        # For substantial responses, create richer summary with metadata
        # This helps verifier see that real work was done
        summary = (
            f"Response ({len(response_text)} chars): "
            f"{response_text[:300]}"
        )
    else:
        summary = response_text[:200]
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO evidence_records
               (id, task_id, source_type, source_ref, tool_name,
                summary, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (eid, task_id, "llm_response", "", None,
             summary,
             json.dumps({"text": response_text[:5000]}, ensure_ascii=False),
             now),
        )

    db._execute_write(_do)
    logger.debug("Evidence %s captured from LLM response for task %s", eid, task_id)
    return eid


def capture_custom(
    task_id: str,
    source_type: str,
    source_ref: str,
    summary: str,
    payload: Any,
    db: Any,
    *,
    tool_name: Optional[str] = None,
) -> str:
    """Capture a custom evidence record."""
    eid = _eid()
    if not isinstance(payload, str):
        payload_str = json.dumps(payload, ensure_ascii=False, default=str)
    else:
        payload_str = payload
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO evidence_records
               (id, task_id, source_type, source_ref, tool_name,
                summary, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (eid, task_id, source_type, source_ref, tool_name,
             summary[:500], _truncate_payload(payload_str), now),
        )

    db._execute_write(_do)
    return eid


# ── Query ─────────────────────────────────────────────────────────


def get_evidence_for_task(task_id: str, db: Any) -> list[dict[str, Any]]:
    """Retrieve all evidence for a task, ordered by creation time."""
    rows = db._conn.execute(
        """SELECT id, source_type, source_ref, tool_name,
                  summary, payload_json, created_at
           FROM evidence_records
           WHERE task_id = ?
           ORDER BY created_at""",
        (task_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_evidence_count(task_id: str, db: Any) -> int:
    """Get count of evidence records for a task."""
    row = db._conn.execute(
        "SELECT COUNT(*) as cnt FROM evidence_records WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return row["cnt"] if row else 0


def get_evidence_summary(task_id: str, db: Any) -> str:
    """Get a compact summary of all evidence for a task."""
    records = get_evidence_for_task(task_id, db)
    if not records:
        return "(no evidence collected)"

    lines = []
    for r in records[:15]:
        source = r["tool_name"] or r["source_type"]
        lines.append(f"- [{source}] {r['summary']}")

    if len(records) > 15:
        lines.append(f"  ... and {len(records) - 15} more records")

    return "\n".join(lines)


# ── Helpers ───────────────────────────────────────────────────────


def _extract_summary(output: str, tool_name: str) -> str:
    """Extract a short summary from tool output."""
    if not output:
        return f"{tool_name}: (empty output)"

    # Try JSON parsing for structured outputs
    try:
        data = json.loads(output)
        if isinstance(data, dict):
            for key in ("summary", "result", "content", "output", "message", "text"):
                if key in data:
                    val = str(data[key])
                    return val[:300]
            return json.dumps(data, ensure_ascii=False)[:300]
        if isinstance(data, list):
            return f"{tool_name}: {len(data)} items"
    except (json.JSONDecodeError, TypeError):
        pass

    return output[:300]


def _truncate_payload(output: str, max_bytes: int = 10000) -> str:
    """Truncate payload to fit storage limits."""
    if not output:
        return ""
    if len(output) <= max_bytes:
        return output
    return output[:max_bytes - 20] + "\n... (truncated)"
