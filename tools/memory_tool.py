#!/usr/bin/env python3
"""
Memory Tool Module - Persistent Curated Memory

Provides bounded, file-backed memory that persists across sessions. Two stores:
  - MEMORY.md: agent's personal notes and observations (environment facts, project
    conventions, tool quirks, things learned)
  - USER.md: what the agent knows about the user (preferences, communication style,
    expectations, workflow habits)

Both are injected into the system prompt as a frozen snapshot at session start.
Mid-session writes update files on disk immediately (durable) but do NOT change
the system prompt -- this preserves the prefix cache for the entire session.
The snapshot refreshes on the next session start.

Entry delimiter: § (section sign). Entries can be multiline.
Character limits (not tokens) because char counts are model-independent.

Design:
- Single `memory` tool with action parameter: add, replace, remove, read
- replace/remove use short unique substring matching (not full text or IDs)
- Behavioral guidance lives in the tool schema description
- Frozen snapshot pattern: system prompt is stable, tool responses show live state
"""

import fcntl
import json
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# Where memory files live
MEMORY_DIR = get_hermes_home() / "memories"

ENTRY_DELIMITER = "\n§\n"


# ---------------------------------------------------------------------------
# Memory content scanning — lightweight check for injection/exfiltration
# in content that gets injected into the system prompt.
# ---------------------------------------------------------------------------

_MEMORY_THREAT_PATTERNS = [
    # Prompt injection
    (r'ignore\s+(previous|all|above|prior)\s+instructions', "prompt_injection"),
    (r'you\s+are\s+now\s+', "role_hijack"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
    (r'act\s+as\s+(if|though)\s+you\s+(have\s+no|don\'t\s+have)\s+(restrictions|limits|rules)', "bypass_restrictions"),
    # Exfiltration via curl/wget with secrets
    (r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', "exfil_curl"),
    (r'wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', "exfil_wget"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)', "read_secrets"),
    # Persistence via shell rc
    (r'authorized_keys', "ssh_backdoor"),
    (r'\$HOME/\.ssh|\~/\.ssh', "ssh_access"),
    (r'\$HOME/\.hermes/\.env|\~/\.hermes/\.env', "hermes_env"),
]

# Subset of invisible chars for injection detection
_INVISIBLE_CHARS = {
    '\u200b', '\u200c', '\u200d', '\u2060', '\ufeff',
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
}


def _scan_memory_content(content: str) -> Optional[str]:
    """Scan memory content for injection/exfil patterns. Returns error string if blocked."""
    # Check invisible unicode
    for char in _INVISIBLE_CHARS:
        if char in content:
            return f"Blocked: content contains invisible unicode character U+{ord(char):04X} (possible injection)."

    # Check threat patterns
    for pattern, pid in _MEMORY_THREAT_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return f"Blocked: content matches threat pattern '{pid}'. Memory entries are injected into the system prompt and must not contain injection or exfiltration payloads."

    return None


class MemoryStore:
    """
    Bounded curated memory with file persistence. One instance per AIAgent.

    Maintains two parallel states:
      - _system_prompt_snapshot: frozen at load time, used for system prompt injection.
        Never mutated mid-session. Keeps prefix cache stable.
      - memory_entries / user_entries: live state, mutated by tool calls, persisted to disk.
        Tool responses always reflect this live state.
    """

    def __init__(self, memory_char_limit: int = 2200, user_char_limit: int = 1375):
        self.memory_entries: List[str] = []
        self.user_entries: List[str] = []
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        # Frozen snapshot for system prompt -- refreshed when files change on disk
        self._system_prompt_snapshot: Dict[str, str] = {"memory": "", "user": ""}
        # Track file mtimes for hot-reload across sessions (TG ↔ CLI sync)
        self._file_mtimes: Dict[str, float] = {}

    def load_from_disk(self):
        """Load entries from MEMORY.md and USER.md, capture system prompt snapshot."""
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)

        self.memory_entries = self._read_file(MEMORY_DIR / "MEMORY.md")
        self.user_entries = self._read_file(MEMORY_DIR / "USER.md")

        # Deduplicate entries (preserves order, keeps first occurrence)
        self.memory_entries = list(dict.fromkeys(self.memory_entries))
        self.user_entries = list(dict.fromkeys(self.user_entries))

        # Capture snapshot for system prompt injection
        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", self.memory_entries),
            "user": self._render_block("user", self.user_entries),
        }
        # Record file mtimes for hot-reload detection
        self._record_mtimes()

    def _record_mtimes(self):
        """Record current mtimes of memory files."""
        for target, fname in (("memory", "MEMORY.md"), ("user", "USER.md")):
            p = MEMORY_DIR / fname
            try:
                self._file_mtimes[target] = p.stat().st_mtime
            except OSError:
                self._file_mtimes[target] = 0.0

    def _files_changed(self) -> bool:
        """Check if memory files have been modified since last load."""
        for target, fname in (("memory", "MEMORY.md"), ("user", "USER.md")):
            p = MEMORY_DIR / fname
            try:
                current_mtime = p.stat().st_mtime
            except OSError:
                current_mtime = 0.0
            if current_mtime != self._file_mtimes.get(target, 0.0):
                return True
        return False

    def maybe_refresh_snapshot(self):
        """Hot-reload: if files changed on disk by an external process
        (e.g. CLI wrote while TG is running), re-read and update the system
        prompt snapshot. Keeps TG ↔ CLI memory in sync without gateway restart.

        Skips reload if this process was the writer (mtime updated by
        _record_mtimes after our own save_to_disk calls)."""
        if not self._files_changed():
            return
        logger.info("Memory files changed on disk — hot-reloading snapshot")
        self.load_from_disk()

    @staticmethod
    @contextmanager
    def _file_lock(path: Path):
        """Acquire an exclusive file lock for read-modify-write safety.

        Uses a separate .lock file so the memory file itself can still be
        atomically replaced via os.replace().
        """
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(lock_path, "w")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()

    @staticmethod
    def _path_for(target: str) -> Path:
        if target == "user":
            return MEMORY_DIR / "USER.md"
        return MEMORY_DIR / "MEMORY.md"

    def _reload_target(self, target: str):
        """Re-read entries from disk into in-memory state.

        Called under file lock to get the latest state before mutating.
        """
        fresh = self._read_file(self._path_for(target))
        fresh = list(dict.fromkeys(fresh))  # deduplicate
        self._set_entries(target, fresh)

    def save_to_disk(self, target: str):
        """Persist entries to the appropriate file. Called after every mutation."""
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        self._write_file(self._path_for(target), self._entries_for(target))
        # Update tracked mtime so our own write doesn't trigger hot-reload
        self._record_mtimes()

    def _entries_for(self, target: str) -> List[str]:
        if target == "user":
            return self.user_entries
        return self.memory_entries

    def _set_entries(self, target: str, entries: List[str]):
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        if not entries:
            return 0
        return len(ENTRY_DELIMITER.join(entries))

    def _char_limit(self, target: str) -> int:
        if target == "user":
            return self.user_char_limit
        return self.memory_char_limit

    def add(self, target: str, content: str) -> Dict[str, Any]:
        """Append a new entry. Returns error if it would exceed the char limit."""
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        # Scan for injection/exfiltration before accepting
        scan_error = _scan_memory_content(content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            # Re-read from disk under lock to pick up writes from other sessions
            self._reload_target(target)

            entries = self._entries_for(target)
            limit = self._char_limit(target)

            # Reject exact duplicates
            if content in entries:
                return self._success_response(target, "Entry already exists (no duplicate added).")

            # Calculate what the new total would be
            new_entries = entries + [content]
            new_total = len(ENTRY_DELIMITER.join(new_entries))

            if new_total > limit:
                current = self._char_count(target)
                return {
                    "success": False,
                    "error": (
                        f"Memory at {current:,}/{limit:,} chars. "
                        f"Adding this entry ({len(content)} chars) would exceed the limit. "
                        f"Replace or remove existing entries first."
                    ),
                    "current_entries": entries,
                    "usage": f"{current:,}/{limit:,}",
                }

            entries.append(content)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry added.")

    def replace(self, target: str, old_text: str, new_content: str) -> Dict[str, Any]:
        """Find entry containing old_text substring, replace it with new_content."""
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {"success": False, "error": "new_content cannot be empty. Use 'remove' to delete entries."}

        # Scan replacement content for injection/exfiltration
        scan_error = _scan_memory_content(new_content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            self._reload_target(target)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if len(matches) == 0:
                return {"success": False, "error": f"No entry matched '{old_text}'."}

            if len(matches) > 1:
                # If all matches are identical (exact duplicates), operate on the first one
                unique_texts = set(e for _, e in matches)
                if len(unique_texts) > 1:
                    previews = [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches]
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }
                # All identical -- safe to replace just the first

            idx = matches[0][0]
            limit = self._char_limit(target)

            # Check that replacement doesn't blow the budget
            test_entries = entries.copy()
            test_entries[idx] = new_content
            new_total = len(ENTRY_DELIMITER.join(test_entries))

            if new_total > limit:
                return {
                    "success": False,
                    "error": (
                        f"Replacement would put memory at {new_total:,}/{limit:,} chars. "
                        f"Shorten the new content or remove other entries first."
                    ),
                }

            entries[idx] = new_content
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> Dict[str, Any]:
        """Remove the entry containing old_text substring."""
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        with self._file_lock(self._path_for(target)):
            self._reload_target(target)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if len(matches) == 0:
                return {"success": False, "error": f"No entry matched '{old_text}'."}

            if len(matches) > 1:
                # If all matches are identical (exact duplicates), remove the first one
                unique_texts = set(e for _, e in matches)
                if len(unique_texts) > 1:
                    previews = [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches]
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }
                # All identical -- safe to remove just the first

            idx = matches[0][0]
            entries.pop(idx)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry removed.")

    def format_for_system_prompt(self, target: str) -> Optional[str]:
        """
        Return memory snapshot for system prompt injection.

        Hot-reloads from disk if files changed (TG ↔ CLI sync).
        Returns None if the snapshot is empty.
        """
        self.maybe_refresh_snapshot()
        block = self._system_prompt_snapshot.get(target, "")
        return block if block else None

    # -- Internal helpers --

    def _success_response(self, target: str, message: str = None) -> Dict[str, Any]:
        entries = self._entries_for(target)
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        resp = {
            "success": True,
            "target": target,
            "entries": entries,
            "usage": f"{pct}% — {current:,}/{limit:,} chars",
            "entry_count": len(entries),
        }
        if message:
            resp["message"] = message
        return resp

    def _render_block(self, target: str, entries: List[str]) -> str:
        """Render a system prompt block with header and usage indicator."""
        if not entries:
            return ""

        limit = self._char_limit(target)
        content = ENTRY_DELIMITER.join(entries)
        current = len(content)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        if target == "user":
            header = f"USER PROFILE (who the user is) [{pct}% — {current:,}/{limit:,} chars]"
        else:
            header = f"MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]"

        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"

    @staticmethod
    def _read_file(path: Path) -> List[str]:
        """Read a memory file and split into entries.

        No file locking needed: _write_file uses atomic rename, so readers
        always see either the previous complete file or the new complete file.
        """
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, IOError):
            return []

        if not raw.strip():
            return []

        # Use ENTRY_DELIMITER for consistency with _write_file. Splitting by "§"
        # alone would incorrectly split entries that contain "§" in their content.
        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
        return [e for e in entries if e]

    @staticmethod
    def _write_file(path: Path, entries: List[str]):
        """Write entries to a memory file using atomic temp-file + rename.

        Previous implementation used open("w") + flock, but "w" truncates the
        file *before* the lock is acquired, creating a race window where
        concurrent readers see an empty file. Atomic rename avoids this:
        readers always see either the old complete file or the new one.
        """
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        try:
            # Write to temp file in same directory (same filesystem for atomic rename)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp", prefix=".mem_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(path))  # Atomic on same filesystem
            except BaseException:
                # Clean up temp file on any failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except (OSError, IOError) as e:
            raise RuntimeError(f"Failed to write memory file {path}: {e}")


def memory_tool(
    action: str,
    target: str = "memory",
    content: str = None,
    old_text: str = None,
    store: Optional[MemoryStore] = None,
) -> str:
    """
    Single entry point for the memory tool. Dispatches to MemoryStore methods.

    Returns JSON string with results.
    """
    if store is None:
        return json.dumps({"success": False, "error": "Memory is not available. It may be disabled in config or this environment."}, ensure_ascii=False)

    if target not in ("memory", "user"):
        return json.dumps({"success": False, "error": f"Invalid target '{target}'. Use 'memory' or 'user'."}, ensure_ascii=False)

    if action == "add":
        if not content:
            return json.dumps({"success": False, "error": "Content is required for 'add' action."}, ensure_ascii=False)
        result = store.add(target, content)

    elif action == "replace":
        if not old_text:
            return json.dumps({"success": False, "error": "old_text is required for 'replace' action."}, ensure_ascii=False)
        if not content:
            return json.dumps({"success": False, "error": "content is required for 'replace' action."}, ensure_ascii=False)
        result = store.replace(target, old_text, content)

    elif action == "remove":
        if not old_text:
            return json.dumps({"success": False, "error": "old_text is required for 'remove' action."}, ensure_ascii=False)
        result = store.remove(target, old_text)

    else:
        return json.dumps({"success": False, "error": f"Unknown action '{action}'. Use: add, replace, remove"}, ensure_ascii=False)

    return json.dumps(result, ensure_ascii=False)


def check_memory_requirements() -> bool:
    """Memory tool has no external requirements -- always available."""
    return True


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

MEMORY_SCHEMA = {
    "name": "memory",
    "description": (
        "Save durable information to persistent memory that survives across sessions. "
        "Memory is injected into future turns, so keep it compact and focused on facts "
        "that will still matter later.\n\n"
        "WHEN TO SAVE (do this proactively, don't wait to be asked):\n"
        "- User corrects you or says 'remember this' / 'don't do that again'\n"
        "- User shares a preference, habit, or personal detail (name, role, timezone, coding style)\n"
        "- You discover something about the environment (OS, installed tools, project structure)\n"
        "- You learn a convention, API quirk, or workflow specific to this user's setup\n"
        "- You identify a stable fact that will be useful again in future sessions\n"
        "- User CONFIRMS a non-obvious approach worked ('yes exactly', 'perfect', accepting an unusual choice) "
        "— record from success AND failure, not just corrections\n\n"
        "PRIORITY: User preferences and corrections > environment facts > procedural knowledge. "
        "The most valuable memory prevents the user from having to repeat themselves.\n\n"
        "ENTRY FORMAT for corrections/feedback:\n"
        "Lead with the rule, then **Why:** (the reason) and **How to apply:** (when it kicks in). "
        "Knowing why lets you judge edge cases instead of blindly following the rule.\n\n"
        "Do NOT save task progress, session outcomes, completed-work logs, or temporary TODO "
        "state to memory; use session_search to recall those from past transcripts.\n"
        "If you've discovered a new way to do something, solved a problem that could be "
        "necessary later, save it as a skill with the skill tool.\n\n"
        "What NOT to save:\n"
        "- Code patterns, conventions, architecture derivable from reading the project\n"
        "- Git history or recent changes (git log/blame are authoritative)\n"
        "- Debugging solutions (the fix is in the code, the commit has context)\n"
        "- Anything already in CLAUDE.md / SOUL.md / project context files\n"
        "- Ephemeral task details or temporary state\n\n"
        "STALENESS: Memory entries are point-in-time. Before recommending based on a memory "
        "that names a specific file, function, or flag — verify it still exists. "
        "'The memory says X exists' is not 'X exists now.'\n\n"
        "TWO TARGETS:\n"
        "- 'user': who the user is -- name, role, preferences, communication style, pet peeves\n"
        "- 'memory': your notes -- environment facts, project conventions, tool quirks, lessons learned\n\n"
        "ACTIONS: add (new entry), replace (update existing -- old_text identifies it), "
        "remove (delete -- old_text identifies it).\n\n"
        "SKIP: trivial/obvious info, things easily re-discovered, raw data dumps, and temporary task state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "replace", "remove"],
                "description": "The action to perform."
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Which memory store: 'memory' for personal notes, 'user' for user profile."
            },
            "content": {
                "type": "string",
                "description": "The entry content. Required for 'add' and 'replace'."
            },
            "old_text": {
                "type": "string",
                "description": "Short unique substring identifying the entry to replace or remove."
            },
        },
        "required": ["action", "target"],
    },
}


# --- Registry ---
from tools.registry import registry

registry.register(
    name="memory",
    toolset="memory",
    schema=MEMORY_SCHEMA,
    handler=lambda args, **kw: memory_tool(
        action=args.get("action", ""),
        target=args.get("target", "memory"),
        content=args.get("content"),
        old_text=args.get("old_text"),
        store=kw.get("store")),
    check_fn=check_memory_requirements,
    emoji="🧠",
)






# =============================================================================
# Agent Memory Reader — operator read-only cross-agent memory access
# =============================================================================

import subprocess as _subprocess

_ZEUS_ROOT = Path(os.environ.get("ZEUS_ROOT", Path.home() / "zeus"))
_AGENT_NAMES = ["ocean", "eleven", "wilson", "susan", "crypto"]


def _agent_eos_path(agent: str) -> Path:
    return _ZEUS_ROOT / "agents" / agent.lower() / "eos"


def _read_agent_memories(agent: str) -> dict:
    """Read MEMORY.md + USER.md from agent eos/memories/."""
    eos = _agent_eos_path(agent)
    result = {"agent": agent.upper(), "memory": "", "user": "", "errors": []}
    for fname, key in [("MEMORY.md", "memory"), ("USER.md", "user")]:
        p = eos / "memories" / fname
        if p.exists():
            try:
                result[key] = p.read_text(encoding="utf-8").strip()
            except Exception as e:
                result["errors"].append(f"{fname}: {e}")
        else:
            result["errors"].append(f"{fname}: not found at {p}")
    return result


def _write_agent_memory(agent: str, target: str, content: str) -> dict:
    """Write MEMORY.md or USER.md to agent eos/memories/ (operator-initiated fix)."""
    eos = _agent_eos_path(agent)
    fname = "MEMORY.md" if target == "memory" else "USER.md"
    p = eos / "memories" / fname
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        # hot-reload: touch mtime so running agent picks it up
        p.touch()
        return {"success": True, "path": str(p), "bytes": len(content.encode())}
    except Exception as e:
        return {"success": False, "error": str(e)}


def read_agent_memory_tool(
    action: str,
    agent: str = "all",
    target: str = "memory",
    content: str = None,
) -> str:
    """
    Operator tool: read or fix apostle agent memories.

    action=read   → return MEMORY.md + USER.md for agent (or all agents)
    action=write  → overwrite agent memory file with content (operator fix)
    """
    agent = agent.strip().lower()

    if action == "read":
        if agent == "all":
            agents = _AGENT_NAMES
        elif agent in _AGENT_NAMES:
            agents = [agent]
        else:
            return json.dumps({"error": f"Unknown agent '{agent}'. Valid: {_AGENT_NAMES}"})

        results = {}
        for a in agents:
            results[a.upper()] = _read_agent_memories(a)
        return json.dumps(results, ensure_ascii=False, indent=2)

    elif action == "write":
        if agent not in _AGENT_NAMES:
            return json.dumps({"error": f"Unknown agent '{agent}'. Valid: {_AGENT_NAMES}"})
        if target not in ("memory", "user"):
            return json.dumps({"error": "target must be 'memory' or 'user'"})
        if not content:
            return json.dumps({"error": "content is required for write action"})
        result = _write_agent_memory(agent, target, content)
        return json.dumps(result, ensure_ascii=False)

    else:
        return json.dumps({"error": f"Unknown action '{action}'. Use: read, write"})


READ_AGENT_MEMORY_SCHEMA = {
    "name": "read_agent_memory",
    "description": (
        "Operator tool: read or fix apostle agent memories (OCEAN/ELEVEN/WILSON/SUSAN/CRYPTO).\n\n"
        "Use action='read' to inspect any agent's MEMORY.md and USER.md.\n"
        "Use action='write' to fix/update an agent's memory file (operator-initiated correction).\n\n"
        "Changes written via action='write' are immediately picked up by the running agent "
        "via hot-reload (mtime detection) — no restart needed.\n\n"
        "Always read first before writing. Preserve existing valid entries when fixing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["read", "write"],
                "description": "read: inspect memory | write: fix/update memory",
            },
            "agent": {
                "type": "string",
                "description": "Agent name: ocean, eleven, wilson, susan, crypto, or 'all' (read only)",
                "default": "all",
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Which file: 'memory' = MEMORY.md, 'user' = USER.md",
                "default": "memory",
            },
            "content": {
                "type": "string",
                "description": "Full replacement content for write action",
            },
        },
        "required": ["action"],
    },
}


def check_read_agent_memory_requirements() -> bool:
    return _ZEUS_ROOT.exists()


registry.register(
    name="read_agent_memory",
    toolset="memory",
    schema=READ_AGENT_MEMORY_SCHEMA,
    handler=lambda args, **kw: read_agent_memory_tool(
        action=args.get("action", "read"),
        agent=args.get("agent", "all"),
        target=args.get("target", "memory"),
        content=args.get("content"),
    ),
    check_fn=check_read_agent_memory_requirements,
    emoji="🔍",
)


# ══════════════════════════════════════════════════════════════════════════════
# HERMES AGENT ADMIN TOOL — 系統維護總管
# 允許 HERMES 讀寫任何 agent 的所有設定檔案
# 包括：config.yaml, SOUL.md, brain weights, EOS hooks, UCB weights 等
# ══════════════════════════════════════════════════════════════════════════════

# 合法的可讀寫根目錄（安全白名單）
_ADMIN_ROOTS = {
    "eos": lambda agent: _ZEUS_ROOT / "agents" / agent.lower() / "eos",
    "hermes": lambda agent: Path.home() / f".hermes-{agent.lower()}",
    "configs": lambda agent: _ZEUS_ROOT / "agents" / agent.lower() / "configs",
    "research": lambda agent: _ZEUS_ROOT / "agents" / agent.lower() / "research",
    "scripts": lambda _: _ZEUS_ROOT / "scripts",
}

# 禁止路徑（防止誤改 sessions/logs/db）
_ADMIN_BLOCKED_PATTERNS = [
    "sessions/", "/logs/", "state.db", ".pyc", "__pycache__",
    "/evidence/", "/learning/", "/summaries/", "/cache/",
]


def _admin_resolve_path(agent: str, path: str) -> tuple[Path, str]:
    """
    Resolve a path like 'eos/config.yaml' → absolute Path.
    Returns (resolved_path, error_msg). error_msg is '' if OK.
    """
    # Check blocked patterns
    for blocked in _ADMIN_BLOCKED_PATTERNS:
        if blocked in path:
            return None, f"Path blocked: '{blocked}' is read-only"

    # Path must start with a known root prefix
    parts = path.split("/", 1)
    root_key = parts[0]
    rel = parts[1] if len(parts) > 1 else ""

    if root_key not in _ADMIN_ROOTS:
        return None, (
            f"Unknown root '{root_key}'. Valid: {list(_ADMIN_ROOTS.keys())}. "
            f"Example paths: 'eos/config.yaml', 'eos/SOUL.md', "
            f"'hermes/config.yaml', 'research/ucb_weights.json'"
        )

    base = _ADMIN_ROOTS[root_key](agent)
    resolved = (base / rel).resolve() if rel else base.resolve()

    # Safety: must stay under the root
    try:
        resolved.relative_to(base.resolve())
    except ValueError:
        return None, f"Path traversal not allowed: {resolved}"

    return resolved, ""


def _admin_list_files(agent: str, path: str) -> list[str]:
    """List files in a directory path."""
    p, err = _admin_resolve_path(agent, path)
    if err:
        return [f"ERROR: {err}"]
    if not p.exists():
        return [f"NOT FOUND: {p}"]
    if p.is_file():
        return [str(p)]
    results = []
    for f in sorted(p.rglob("*")):
        if f.is_file():
            rel = str(f.relative_to(p.parent.parent if path.count("/") == 0 else p.parent))
            blocked = any(b in str(f) for b in _ADMIN_BLOCKED_PATTERNS)
            if not blocked:
                results.append(rel)
    return results[:100]


def agent_admin_tool(
    action: str,
    agent: str,
    path: str = "",
    content: str = None,
) -> str:
    """
    HERMES 系統維護總管 tool.

    action=list   → list files under agent path
    action=read   → read file content
    action=write  → write/overwrite file (hot-reload aware)
    action=patch  → patch file: find old_text and replace with new_text (pass as JSON in content)
    """
    agent = agent.strip().lower()
    if agent not in _AGENT_NAMES and agent != "hermes":
        return json.dumps({"error": f"Unknown agent '{agent}'. Valid: {_AGENT_NAMES + ['hermes']}"})

    if action == "list":
        if not path:
            # list all roots for this agent
            roots_info = {}
            for rk in _ADMIN_ROOTS:
                base = _ADMIN_ROOTS[rk](agent)
                roots_info[rk] = str(base) + (" (exists)" if base.exists() else " (missing)")
            return json.dumps({"agent": agent.upper(), "roots": roots_info}, ensure_ascii=False, indent=2)
        files = _admin_list_files(agent, path)
        return json.dumps({"agent": agent.upper(), "path": path, "files": files}, ensure_ascii=False, indent=2)

    elif action == "read":
        if not path:
            return json.dumps({"error": "path is required for read action"})
        p, err = _admin_resolve_path(agent, path)
        if err:
            return json.dumps({"error": err})
        if not p.exists():
            return json.dumps({"error": f"File not found: {p}"})
        if p.is_dir():
            return json.dumps({"error": f"Path is a directory. Use action=list"})
        try:
            content_read = p.read_text(encoding="utf-8")
            return json.dumps({
                "agent": agent.upper(), "path": path,
                "abs_path": str(p), "size": len(content_read),
                "content": content_read
            }, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"error": f"Read failed: {e}"})

    elif action == "write":
        if not path:
            return json.dumps({"error": "path is required for write action"})
        if content is None:
            return json.dumps({"error": "content is required for write action"})
        p, err = _admin_resolve_path(agent, path)
        if err:
            return json.dumps({"error": err})
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            p.touch()  # hot-reload: update mtime
            return json.dumps({
                "success": True, "agent": agent.upper(),
                "path": path, "abs_path": str(p),
                "bytes_written": len(content.encode())
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"Write failed: {e}"})

    elif action == "patch":
        if not path:
            return json.dumps({"error": "path is required for patch action"})
        if not content:
            return json.dumps({"error": "content must be JSON: {\"old\": \"...\", \"new\": \"...\"}"})
        try:
            patch_data = json.loads(content)
            old_text = patch_data.get("old", "")
            new_text = patch_data.get("new", "")
        except Exception:
            return json.dumps({"error": "content must be valid JSON: {\"old\": \"...\", \"new\": \"...\"}"})
        p, err = _admin_resolve_path(agent, path)
        if err:
            return json.dumps({"error": err})
        if not p.exists():
            return json.dumps({"error": f"File not found: {p}"})
        try:
            original = p.read_text(encoding="utf-8")
            if old_text not in original:
                return json.dumps({"error": f"old_text not found in file", "hint": original[:200]})
            patched = original.replace(old_text, new_text, 1)
            p.write_text(patched, encoding="utf-8")
            p.touch()
            return json.dumps({
                "success": True, "agent": agent.upper(),
                "path": path, "replacements": 1,
                "size_before": len(original), "size_after": len(patched)
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"Patch failed: {e}"})

    else:
        return json.dumps({"error": f"Unknown action '{action}'. Use: list, read, write, patch"})


AGENT_ADMIN_SCHEMA = {
    "name": "agent_admin",
    "description": (
        "HERMES 系統維護總管 — 讀寫任何 agent 的設定檔案。\n\n"
        "可操作的路徑根目錄：\n"
        "  eos/        → ~/zeus/agents/{agent}/eos/ (config.yaml, SOUL.md, hooks/, etc.)\n"
        "  hermes/     → ~/.hermes-{agent}/ (gateway config.yaml, memories/)\n"
        "  configs/    → ~/zeus/agents/{agent}/configs/ (strategy configs)\n"
        "  research/   → ~/zeus/agents/{agent}/research/ (ucb_weights.json, etc.)\n"
        "  scripts/    → ~/zeus/scripts/ (shared scripts, 任何 agent 通用)\n\n"
        "常用範例：\n"
        "  讀取 OCEAN config: action=read, agent=ocean, path=eos/config.yaml\n"
        "  修改 WILSON conviction: action=patch, agent=wilson, path=eos/config.yaml, "
        "content='{\"old\":\"conviction_threshold: 0.6\",\"new\":\"conviction_threshold: 0.5\"}'\n"
        "  寫入 ELEVEN SOUL.md: action=write, agent=eleven, path=eos/SOUL.md, content=...\n"
        "  列出 CRYPTO research: action=list, agent=crypto, path=research/\n\n"
        "注意：sessions/, logs/, state.db, cache/ 等執行期資料為唯讀，不可寫入。\n"
        "寫入後自動觸發 mtime hot-reload，running agent 立即感知，不需重啟。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "read", "write", "patch"],
                "description": "list: 列出檔案 | read: 讀取內容 | write: 覆寫檔案 | patch: 局部替換",
            },
            "agent": {
                "type": "string",
                "description": "Agent 名稱: ocean, eleven, wilson, susan, crypto",
            },
            "path": {
                "type": "string",
                "description": "相對路徑，格式: {root}/{file}，例如 eos/config.yaml",
                "default": "",
            },
            "content": {
                "type": "string",
                "description": (
                    "write: 完整新內容 | "
                    "patch: JSON 格式 {\"old\": \"原文\", \"new\": \"新文\"}"
                ),
            },
        },
        "required": ["action", "agent"],
    },
}


def check_agent_admin_requirements() -> bool:
    return _ZEUS_ROOT.exists()


registry.register(
    name="agent_admin",
    toolset="memory",
    schema=AGENT_ADMIN_SCHEMA,
    handler=lambda args, **kw: agent_admin_tool(
        action=args.get("action", "list"),
        agent=args.get("agent", ""),
        path=args.get("path", ""),
        content=args.get("content"),
    ),
    check_fn=check_agent_admin_requirements,
    emoji="🔧",
)
