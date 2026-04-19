"""Hermes Memory V2 Runtime Integration.

Bridges the V2 storage layer (core/working/archival/SQLite FTS) into the
Hermes agent runtime:
  - build_memory_v2_prompt()     → inject L1+L2+L3 into system prompt
  - search_memory()              → FTS recall from local agent memory.db
  - update_working_memory()      → per-turn L2 writeback
  - rollover_working_memory()    → compact handoff on session rotation
  - persist_session_summary()    → write summary JSON + SQLite + FTS
  - summarize_memory_turn()      → extract working memory fields from turn
  - promote_stable_facts()       → move stable facts into L1 core memory

All paths resolve relative to HERMES_HOME so each agent is fully isolated.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger("hermes.memory_v2")

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _agent_home() -> Path:
    return get_hermes_home()

def _memory_dir() -> Path:
    return _agent_home() / "memory"

def _working_dir() -> Path:
    d = _agent_home() / "working"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _summaries_dir() -> Path:
    d = _agent_home() / "summaries"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _store_dir() -> Path:
    d = _agent_home() / "store"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _db_path() -> Path:
    return _store_dir() / "memory.db"

def _archive_dir() -> Path:
    d = _agent_home() / "archive" / "working_history"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


# ---------------------------------------------------------------------------
# L1: Core Memory — read
# ---------------------------------------------------------------------------

def _load_core_user_memory() -> List[Dict[str, str]]:
    """Load core_user_memory.json facts."""
    data = _read_json(_memory_dir() / "core_user_memory.json")
    return data.get("facts", [])

def _load_agent_core_memory() -> List[Dict[str, str]]:
    """Load agent_core_memory.json facts."""
    data = _read_json(_memory_dir() / "agent_core_memory.json")
    return data.get("facts", [])

def _extract_fact_lines(facts: List[Dict[str, str]]) -> List[str]:
    """Convert fact dicts to displayable lines."""
    lines = []
    for f in facts:
        key = f.get("key", "")
        value = f.get("value", "")
        if key and value:
            lines.append(f"- {key}: {value}")
        elif value:
            lines.append(f"- {value}")
    return lines


# ---------------------------------------------------------------------------
# L2: Working Memory — read/write
# ---------------------------------------------------------------------------

def load_working_memory(session_id: str) -> Dict[str, Any]:
    """Load working memory for a session."""
    path = _working_dir() / f"{session_id}.json"
    return _read_json(path)

def update_working_memory(session_id: str, turn_data: Dict[str, Any]) -> None:
    """Write working memory after a turn.

    turn_data should contain: current_task, recent_decisions, open_loops,
    next_actions, stable_facts_candidate (all optional).
    """
    path = _working_dir() / f"{session_id}.json"
    existing = _read_json(path)

    wm = {
        "schema_version": 1,
        "memory_type": "working_memory",
        "session_id": session_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "current_task": turn_data.get("current_task", existing.get("current_task", "")),
        "recent_decisions": turn_data.get("recent_decisions", existing.get("recent_decisions", [])),
        "open_loops": turn_data.get("open_loops", existing.get("open_loops", [])),
        "next_actions": turn_data.get("next_actions", existing.get("next_actions", [])),
        "handoff_from": existing.get("handoff_from"),
        "stable_facts_candidate": turn_data.get("stable_facts_candidate", existing.get("stable_facts_candidate", [])),
        "promoted_facts": existing.get("promoted_facts", []),
    }
    _write_json(path, wm)


def rollover_working_memory(old_session_id: str, new_session_id: str) -> None:
    """Create compact handoff from old session and seed new working memory."""
    old_wm = load_working_memory(old_session_id)

    # Archive old working memory
    old_path = _working_dir() / f"{old_session_id}.json"
    if old_path.exists():
        archive_path = _archive_dir() / f"{old_session_id}.json"
        old_path.rename(archive_path)

    # Create new working memory with handoff
    handoff = {
        "schema_version": 1,
        "memory_type": "working_memory",
        "session_id": new_session_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "current_task": "",
        "recent_decisions": [],
        "open_loops": old_wm.get("open_loops", []),
        "next_actions": old_wm.get("next_actions", []),
        "handoff_from": old_session_id,
        "stable_facts_candidate": [],
        "promoted_facts": [],
    }
    _write_json(_working_dir() / f"{new_session_id}.json", handoff)


# ---------------------------------------------------------------------------
# L3: Session Summary — write
# ---------------------------------------------------------------------------

def persist_session_summary(
    agent_id: str,
    session_id: str,
    summary_data: Dict[str, Any],
) -> None:
    """Write session summary to JSON file + SQLite + FTS."""
    payload = {
        "schema_version": 1,
        "agent_id": agent_id,
        "session_id": session_id,
        "topic": summary_data.get("topic", ""),
        "summary": summary_data.get("summary", ""),
        "outcome": summary_data.get("outcome", ""),
        "open_loops": summary_data.get("open_loops", []),
        "files": summary_data.get("files", []),
        "tags": summary_data.get("tags", []),
        "message_count": summary_data.get("message_count", 0),
        "stable_facts_candidate": summary_data.get("stable_facts_candidate", []),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # Write JSON
    _write_json(_summaries_dir() / f"{session_id}.json", payload)

    # Write SQLite
    try:
        db = sqlite3.connect(str(_db_path()))
        db.execute("""
            INSERT OR REPLACE INTO session_summaries
            (session_id, agent_id, topic, summary, outcome,
             open_loops_json, files_json, tags_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session_id, agent_id,
            payload["topic"], payload["summary"], payload["outcome"],
            json.dumps(payload["open_loops"]),
            json.dumps(payload["files"]),
            json.dumps(payload["tags"]),
            payload["created_at"],
        ))
        # FTS
        db.execute("""
            INSERT OR REPLACE INTO session_summaries_fts
            (session_id, agent_id, summary)
            VALUES (?, ?, ?)
        """, (session_id, agent_id, payload["summary"]))
        db.commit()
        db.close()
    except Exception as e:
        logger.warning("persist_session_summary SQLite failed: %s", e)


# ---------------------------------------------------------------------------
# L3: Search / Recall
# ---------------------------------------------------------------------------

def search_memory(query: str, agent_id: str = "") -> Dict[str, Optional[str]]:
    """Search local agent memory.db with fixed budget.

    Returns up to: 1 summary, 1 handoff, 1 archival chunk.
    """
    result: Dict[str, Optional[str]] = {
        "summary": None,
        "handoff": None,
        "archival": None,
    }

    # 1. Search session summaries (FTS)
    result["summary"] = _search_session_summaries(query, agent_id)

    # 2. Search working handoff
    result["handoff"] = _search_working_handoff(query)

    # 3. Search archival chunks (FTS)
    used_session_ids = set()
    if result["summary"]:
        # Extract session_id from summary to avoid dup
        pass  # archival dedup is best-effort
    result["archival"] = _search_archival_chunks(query, agent_id)

    return result


def _search_session_summaries(query: str, agent_id: str = "") -> Optional[str]:
    """FTS search over session_summaries. Returns best match text."""
    try:
        db = sqlite3.connect(str(_db_path()))
        rows = db.execute("""
            SELECT s.session_id, s.topic, s.summary, s.created_at
            FROM session_summaries_fts f
            JOIN session_summaries s ON f.session_id = s.session_id
            WHERE session_summaries_fts MATCH ?
            ORDER BY s.created_at DESC
            LIMIT 1
        """, (query,)).fetchall()
        db.close()
        if rows:
            sid, topic, summary, ts = rows[0]
            return f"[Session {sid} | {topic}] {summary}"
    except Exception as e:
        logger.debug("_search_session_summaries: %s", e)
    return None


def _search_working_handoff(query: str) -> Optional[str]:
    """Find the most recent working memory with handoff_from set."""
    try:
        working = _working_dir()
        candidates = []
        for f in sorted(working.glob("*.json"), reverse=True):
            data = _read_json(f)
            if data.get("handoff_from"):
                candidates.append(data)
                break
        if candidates:
            wm = candidates[0]
            parts = []
            if wm.get("open_loops"):
                parts.append(f"open_loops: {', '.join(wm['open_loops'][:3])}")
            if wm.get("next_actions"):
                parts.append(f"next_actions: {', '.join(wm['next_actions'][:3])}")
            if parts:
                return f"[Handoff from {wm['handoff_from']}] {'; '.join(parts)}"
    except Exception as e:
        logger.debug("_search_working_handoff: %s", e)
    return None


def _search_archival_chunks(query: str, agent_id: str = "") -> Optional[str]:
    """FTS search over archival_chunks. Returns best match text."""
    try:
        db = sqlite3.connect(str(_db_path()))
        rows = db.execute("""
            SELECT c.session_id, c.chunk_text, c.created_at
            FROM archival_chunks_fts f
            JOIN archival_chunks c ON f.rowid = c.id
            WHERE archival_chunks_fts MATCH ?
            ORDER BY c.created_at DESC
            LIMIT 1
        """, (query,)).fetchall()
        db.close()
        if rows:
            sid, text, ts = rows[0]
            return f"[Archive {sid}] {text[:500]}"
    except Exception as e:
        logger.debug("_search_archival_chunks: %s", e)
    return None


# ---------------------------------------------------------------------------
# Prompt Assembly
# ---------------------------------------------------------------------------

def build_memory_v2_prompt(session_id: str, user_message: str = "") -> str:
    """Build the memory block for system prompt injection.

    Combines L1 (core) + L2 (working) + L3 (recall).
    """
    parts: List[str] = []

    # L1: Core Memory
    user_facts = _extract_fact_lines(_load_core_user_memory())
    agent_facts = _extract_fact_lines(_load_agent_core_memory())

    if user_facts or agent_facts:
        parts.append("══ Core Memory ══")
        if user_facts:
            parts.append("User:\n" + "\n".join(user_facts))
        if agent_facts:
            parts.append("Agent:\n" + "\n".join(agent_facts))

    # L2: Working Memory
    wm = load_working_memory(session_id)
    if wm:
        wm_lines = []
        if wm.get("current_task"):
            wm_lines.append(f"Task: {wm['current_task']}")
        if wm.get("recent_decisions"):
            wm_lines.append(f"Decisions: {', '.join(wm['recent_decisions'][:3])}")
        if wm.get("open_loops"):
            wm_lines.append(f"Open: {', '.join(wm['open_loops'][:3])}")
        if wm.get("next_actions"):
            wm_lines.append(f"Next: {', '.join(wm['next_actions'][:3])}")
        if wm_lines:
            parts.append("══ Working Memory ══\n" + "\n".join(wm_lines))

    # L3: Recall (only if we have a query)
    if user_message:
        # Use first 100 chars as recall query
        query = user_message[:100].strip()
        if query:
            recall = search_memory(query)
            recall_lines = []
            if recall.get("summary"):
                recall_lines.append(recall["summary"])
            if recall.get("handoff"):
                recall_lines.append(recall["handoff"])
            if recall.get("archival"):
                recall_lines.append(recall["archival"])
            if recall_lines:
                parts.append("══ Recall ══\n" + "\n".join(recall_lines))

    if not parts:
        return ""

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Turn Summarization (lightweight — no LLM, just extraction)
# ---------------------------------------------------------------------------

def summarize_memory_turn(
    user_message: str,
    assistant_response: str,
    session_id: str,
) -> Dict[str, Any]:
    """Extract working memory fields from the latest turn.

    This is a lightweight extraction — no LLM call. It preserves
    the last user intent and assistant action for working memory.
    """
    # Simple heuristic extraction
    current_task = user_message[:200] if user_message else ""
    recent_decisions = []
    if assistant_response:
        # Extract first actionable line
        for line in assistant_response.split("\n")[:5]:
            line = line.strip()
            if line and len(line) > 10 and not line.startswith("#"):
                recent_decisions.append(line[:150])
                break

    return {
        "current_task": current_task,
        "recent_decisions": recent_decisions,
        "open_loops": [],
        "next_actions": [],
        "stable_facts_candidate": [],
    }


# ---------------------------------------------------------------------------
# Stable Fact Promotion
# ---------------------------------------------------------------------------

def promote_stable_facts(facts: List[Dict[str, str]], target: str = "user") -> int:
    """Promote facts into core memory. Returns count of new facts added."""
    if target == "user":
        path = _memory_dir() / "core_user_memory.json"
    else:
        path = _memory_dir() / "agent_core_memory.json"

    data = _read_json(path)
    existing_keys = {f.get("key") for f in data.get("facts", [])}

    added = 0
    for fact in facts:
        if fact.get("key") and fact["key"] not in existing_keys:
            data.setdefault("facts", []).append({
                "key": fact["key"],
                "value": fact.get("value", ""),
                "source": "memory_v2_promotion",
                "priority": fact.get("priority", "normal"),
            })
            existing_keys.add(fact["key"])
            added += 1

    if added:
        _write_json(path, data)
        logger.info("Promoted %d facts to %s core memory", added, target)

    return added


# ---------------------------------------------------------------------------
# V2 availability check
# ---------------------------------------------------------------------------

def is_memory_v2_available() -> bool:
    """Check if Memory V2 storage is initialized for this agent."""
    mem_dir = _memory_dir()
    return (
        (mem_dir / "core_user_memory.json").exists()
        or (mem_dir / "agent_core_memory.json").exists()
    )
