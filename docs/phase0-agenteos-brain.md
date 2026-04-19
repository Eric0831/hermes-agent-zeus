# Phase 0: Adding the AgentEOS Brain to Hermes

## Engineering Specification — v0.1

**Goal**: Insert the missing "central brain" (Executive Controller, Planner, Verifier,
Evidence Store) into Hermes **without rewriting** existing Gateway/Tools/Memory code.

**Timeline**: 4-6 weeks, 4 sprints

**Principle**: Hermes already has strong peripherals (Gateway, Tools, Memory, Approval).
Phase 0 adds the **cognitive control layer** between event intake and tool execution.

---

## 0. What Changes, What Doesn't

### Stays the same
- Gateway platform adapters (12+ platforms)
- Tool registry + 40+ tools + schema validation
- Three-tier memory (Core/Working/Archival)
- Dangerous command detection + approval
- Skills system
- Cron scheduler
- SQLite state.db (sessions + messages)
- Smart model routing
- Cost tracking

### New modules (added alongside existing code)
- `brain/executive.py` — Event triage + task lifecycle
- `brain/planner.py` — Structured task graph generation
- `brain/verifier.py` — Task completion verification
- `brain/evidence.py` — Evidence store operations
- `brain/policy.py` — Unified policy evaluation
- `brain/world_state.py` — Task-centric world state
- `brain/task_store.py` — Task CRUD + state machine DB ops
- `brain/models.py` — Shared dataclasses

### Modified files (surgical insertions)
- `gateway/run.py` — Insert Executive triage at `_handle_message_with_agent()`
- `agent/task_state.py` — Extend states for planned tasks
- `hermes_state.py` — Add new tables (tasks, evidence, etc.)

---

## 1. Architecture: Before and After

### BEFORE (current Hermes)
```
Message → Gateway → Session lookup → LLM loop (run_conversation) → Response
                                        ↕
                                    Tool dispatch
```

### AFTER (Phase 0)
```
Message → Gateway → Session lookup
                        ↓
                  Executive Controller
                    ├── DIRECT: simple Q&A → existing LLM loop → Response
                    └── TASK: complex work → Planner
                                               ↓
                                          Task Graph (DB)
                                               ↓
                                          LLM loop (with task context)
                                               ↕
                                          Tool dispatch → Evidence Store
                                               ↓
                                           Verifier
                                            ├── pass → Response + World State update
                                            ├── fail_retriable → retry subtask
                                            └── fail_non_retriable → failure response
```

---

## 2. New Database Schema

All new tables live in the **existing `state.db`** SQLite database.
Migration handled by incrementing `SCHEMA_VERSION` to 7.

### 2.1 `tasks`
```sql
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    parent_task_id TEXT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    event_text TEXT,
    task_type TEXT NOT NULL DEFAULT 'general',
    -- 'direct_reply' | 'short_task' | 'research' | 'coding' | 'summary' | 'general'
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'received',
    -- received | triaged | planned | running | verifying |
    -- completed | failed | blocked | cancelled
    priority TEXT NOT NULL DEFAULT 'medium',
    risk_level TEXT NOT NULL DEFAULT 'low',
    plan_json TEXT,          -- JSON: planner output (goal, criteria, subtasks)
    budget_tokens INTEGER,
    budget_ms INTEGER,
    requires_approval BOOLEAN NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 2,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    failure_reason TEXT,
    verification_status TEXT,  -- pass | fail_retriable | fail_non_retriable | needs_human
    verification_json TEXT,    -- JSON: verifier output
    FOREIGN KEY (parent_task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);
```

### 2.2 `task_criteria`
```sql
CREATE TABLE IF NOT EXISTS task_criteria (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    criterion_key TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    -- pending | met | unmet | skipped
    evidence_ids TEXT,  -- JSON array of evidence IDs
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_criteria_task ON task_criteria(task_id);
```

### 2.3 `evidence_records`
```sql
CREATE TABLE IF NOT EXISTS evidence_records (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    source_type TEXT NOT NULL,
    -- 'tool_output' | 'llm_response' | 'file_content' | 'search_result' | 'test_result'
    source_ref TEXT,         -- tool_call_id, file path, URL, etc.
    tool_name TEXT,
    summary TEXT,
    payload_json TEXT,       -- full tool output or relevant excerpt
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_task ON evidence_records(task_id);
```

### 2.4 `task_transitions`
```sql
CREATE TABLE IF NOT EXISTS task_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    reason TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transitions_task ON task_transitions(task_id);
```

### 2.5 `policy_evaluations`
```sql
CREATE TABLE IF NOT EXISTS policy_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    action_type TEXT NOT NULL,
    target TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    decision TEXT NOT NULL,
    -- allow | deny | allow_with_approval | sandbox_required
    reason TEXT,
    created_at REAL NOT NULL
);
```

---

## 3. New Module Specifications

### 3.1 `brain/models.py` — Shared Data Structures

```python
"""AgentEOS brain data models."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional
import time
import uuid


def _now() -> float:
    return time.time()

def _id(prefix: str = "task") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class TriageResult:
    """Output of Executive Controller triage."""
    decision: str          # 'direct_reply' | 'create_task'
    reason: str
    task_type: str = "general"
    priority: str = "medium"
    risk_level: str = "low"
    requires_approval: bool = False
    budget_tokens: int | None = None
    budget_ms: int | None = None


@dataclass
class PlanSpec:
    """Structured plan output from Planner."""
    goal: str
    success_criteria: list[str]
    subtasks: list[SubtaskSpec] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    recommended_tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "success_criteria": self.success_criteria,
            "subtasks": [s.to_dict() for s in self.subtasks],
            "risks": self.risks,
            "recommended_tools": self.recommended_tools,
        }


@dataclass
class SubtaskSpec:
    """A single subtask in a plan."""
    id: str
    description: str
    tool: str | None = None
    depends_on: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "tool": self.tool,
            "depends_on": self.depends_on,
        }


@dataclass
class VerificationResult:
    """Output of Verifier."""
    status: str            # 'pass' | 'fail_retriable' | 'fail_non_retriable' | 'needs_human'
    criteria_results: list[CriterionResult] = field(default_factory=list)
    summary: str = ""
    missing_evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "criteria_results": [c.to_dict() for c in self.criteria_results],
            "summary": self.summary,
            "missing_evidence": self.missing_evidence,
        }


@dataclass
class CriterionResult:
    """Verification result for a single criterion."""
    criterion_key: str
    description: str
    status: str            # 'met' | 'unmet' | 'skipped'
    evidence_summary: str = ""

    def to_dict(self) -> dict:
        return {
            "criterion_key": self.criterion_key,
            "description": self.description,
            "status": self.status,
            "evidence_summary": self.evidence_summary,
        }


@dataclass
class PolicyDecision:
    """Output of Policy evaluation."""
    decision: str          # 'allow' | 'deny' | 'allow_with_approval' | 'sandbox_required'
    reason: str
    risk_level: str = "low"
```

### 3.2 `brain/executive.py` — Executive Controller

**Purpose**: Triage incoming messages — decide whether to do a direct LLM reply
or create a structured task. This is the "prefrontal cortex."

```python
"""Executive Controller — event triage and task lifecycle."""

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Heuristic thresholds (tunable, no LLM needed)
_SHORT_MSG_CHARS = 120
_TASK_KEYWORDS = [
    "research", "compare", "analyze", "investigate", "find all",
    "write a", "create a", "build", "implement", "fix", "debug",
    "summarize", "review", "check", "test", "deploy",
    "整理", "分析", "比較", "研究", "調查", "寫", "建立", "修改", "測試",
]
_HIGH_RISK_TOOLS = ["shell_exec", "terminal", "send_message", "deploy"]


def triage(
    message: str,
    *,
    has_media: bool = False,
    session_history_len: int = 0,
    active_task_count: int = 0,
) -> "TriageResult":
    """
    Decide: direct reply or create task?

    Phase 0 uses heuristics. Phase 1 can add LLM-based intent classification.

    Returns TriageResult with decision and metadata.
    """
    from brain.models import TriageResult

    text = message.strip().lower()
    text_len = len(message.strip())

    # Rule 1: Very short messages without task keywords → direct reply
    if text_len < _SHORT_MSG_CHARS and not _has_task_intent(text):
        return TriageResult(
            decision="direct_reply",
            reason="short_message_no_task_intent",
            task_type="direct_reply",
        )

    # Rule 2: Questions without action verbs → direct reply
    if _is_simple_question(text) and not _has_task_intent(text):
        return TriageResult(
            decision="direct_reply",
            reason="simple_question",
            task_type="direct_reply",
        )

    # Rule 3: Has task-like intent → create task
    if _has_task_intent(text):
        task_type = _classify_task_type(text)
        risk = _estimate_risk(text)
        return TriageResult(
            decision="create_task",
            reason="task_intent_detected",
            task_type=task_type,
            priority="medium",
            risk_level=risk,
            requires_approval=risk == "high",
        )

    # Rule 4: Long message with media → likely a task
    if text_len > 300 or has_media:
        return TriageResult(
            decision="create_task",
            reason="complex_input",
            task_type="general",
        )

    # Default: direct reply (conservative — don't over-task)
    return TriageResult(
        decision="direct_reply",
        reason="default_direct",
        task_type="direct_reply",
    )


def _has_task_intent(text: str) -> bool:
    return any(kw in text for kw in _TASK_KEYWORDS)


def _is_simple_question(text: str) -> bool:
    q_markers = ["?", "？", "what is", "who is", "how does", "什麼是", "怎麼"]
    return any(m in text for m in q_markers) and len(text) < 200


def _classify_task_type(text: str) -> str:
    if any(w in text for w in ["research", "compare", "investigate", "分析", "比較", "研究"]):
        return "research"
    if any(w in text for w in ["code", "fix", "debug", "implement", "build", "修改", "修復", "寫程式"]):
        return "coding"
    if any(w in text for w in ["summarize", "summary", "整理", "摘要"]):
        return "summary"
    return "general"


def _estimate_risk(text: str) -> str:
    high_risk = ["deploy", "delete", "drop", "rm ", "kill", "部署", "刪除"]
    if any(w in text for w in high_risk):
        return "high"
    medium_risk = ["send", "write", "modify", "update", "發送", "修改"]
    if any(w in text for w in medium_risk):
        return "medium"
    return "low"
```

### 3.3 `brain/planner.py` — Planner Engine

**Purpose**: Take a task goal and produce a structured plan with success criteria.
Uses LLM with constrained JSON output.

```python
"""Planner Engine — structured task graph generation."""

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """You are a task planner. Given a user's goal, produce a structured plan.

Output ONLY valid JSON with this schema:
{
  "goal": "restate the goal clearly",
  "success_criteria": ["criterion 1", "criterion 2", ...],
  "subtasks": [
    {"id": "s1", "description": "...", "tool": "tool_name_or_null", "depends_on": []},
    {"id": "s2", "description": "...", "tool": null, "depends_on": ["s1"]}
  ],
  "risks": ["risk 1", ...],
  "recommended_tools": ["tool1", "tool2"]
}

Rules:
- success_criteria: 2-5 concrete, verifiable conditions
- subtasks: 1-8 steps, max depth 2
- Each criterion must be objectively checkable
- If the task is simple, use fewer subtasks
- risks: potential failure points (0-3)
- recommended_tools: from available tools list
"""

MAX_SUBTASKS = 8
MAX_CRITERIA = 5


def generate_plan(
    goal: str,
    *,
    task_type: str = "general",
    available_tools: list[str] | None = None,
    context: str = "",
    llm_call: callable = None,
) -> "PlanSpec":
    """
    Generate a structured plan for a task.

    Args:
        goal: The user's stated goal
        task_type: research | coding | summary | general
        available_tools: List of tool names the agent can use
        context: Additional context (world state, memory)
        llm_call: Callable that takes (system_prompt, user_message) -> str

    Returns:
        PlanSpec with goal, criteria, subtasks, risks
    """
    from brain.models import PlanSpec, SubtaskSpec

    if llm_call is None:
        # Fallback: minimal plan without LLM
        return _fallback_plan(goal, task_type)

    tools_hint = ""
    if available_tools:
        tools_hint = f"\nAvailable tools: {', '.join(available_tools[:20])}"

    user_prompt = f"""Task type: {task_type}
Goal: {goal}
{tools_hint}
{f"Context: {context}" if context else ""}

Generate the plan as JSON."""

    try:
        raw = llm_call(PLANNER_SYSTEM_PROMPT, user_prompt)
        plan_data = _parse_plan_json(raw)
        return _validate_plan(plan_data)
    except Exception as e:
        logger.warning("Planner LLM failed, using fallback: %s", e)
        return _fallback_plan(goal, task_type)


def _fallback_plan(goal: str, task_type: str) -> "PlanSpec":
    """Minimal plan when LLM is unavailable."""
    from brain.models import PlanSpec, SubtaskSpec

    return PlanSpec(
        goal=goal,
        success_criteria=[f"Task '{goal}' completed and output provided"],
        subtasks=[
            SubtaskSpec(id="s1", description=f"Execute: {goal}")
        ],
        risks=[],
    )


def _parse_plan_json(raw: str) -> dict:
    """Extract JSON from LLM response (handles markdown fences)."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    return json.loads(text)


def _validate_plan(data: dict) -> "PlanSpec":
    """Validate and cap plan to safe limits."""
    from brain.models import PlanSpec, SubtaskSpec

    criteria = data.get("success_criteria", [])[:MAX_CRITERIA]
    if not criteria:
        criteria = [f"Task completed: {data.get('goal', 'unknown')}"]

    subtasks_raw = data.get("subtasks", [])[:MAX_SUBTASKS]
    subtasks = []
    for s in subtasks_raw:
        subtasks.append(SubtaskSpec(
            id=s.get("id", f"s{len(subtasks)+1}"),
            description=s.get("description", ""),
            tool=s.get("tool"),
            depends_on=s.get("depends_on", []),
        ))

    if not subtasks:
        subtasks = [SubtaskSpec(id="s1", description=data.get("goal", "execute task"))]

    return PlanSpec(
        goal=data.get("goal", ""),
        success_criteria=criteria,
        subtasks=subtasks,
        risks=data.get("risks", [])[:3],
        recommended_tools=data.get("recommended_tools", []),
    )
```

### 3.4 `brain/verifier.py` — Verifier & Auditor

**Purpose**: After task execution, check whether success criteria are met
with actual evidence. Prevents "model says done = done."

```python
"""Verifier — task completion verification with evidence checking."""

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

VERIFIER_SYSTEM_PROMPT = """You are a task verifier. Given:
1. The original goal and success criteria
2. The evidence collected during execution
3. The assistant's final response

Evaluate whether each criterion is MET or UNMET.

Output ONLY valid JSON:
{
  "criteria_results": [
    {"criterion_key": "c0", "description": "...", "status": "met|unmet", "evidence_summary": "..."}
  ],
  "overall_status": "pass|fail_retriable|fail_non_retriable",
  "summary": "one line summary of verification result",
  "missing_evidence": ["what's missing if any"]
}

Rules:
- "met": clear evidence supports the criterion
- "unmet": no evidence, or evidence contradicts the criterion
- "pass": ALL criteria met
- "fail_retriable": some criteria unmet but could be fixed with another attempt
- "fail_non_retriable": fundamental failure (wrong approach, impossible task)
"""


def verify_task(
    goal: str,
    criteria: list[dict],
    evidence: list[dict],
    final_response: str,
    *,
    llm_call: callable = None,
) -> "VerificationResult":
    """
    Verify whether a task's success criteria are met.

    Args:
        goal: The task goal
        criteria: List of {"criterion_key": str, "description": str}
        evidence: List of evidence records
        final_response: The agent's final text response
        llm_call: Optional LLM for deeper verification

    Returns:
        VerificationResult
    """
    from brain.models import VerificationResult, CriterionResult

    # Phase 0 Level 1: Rule-based checks
    results = []
    for c in criteria:
        cr = _check_criterion_heuristic(c, evidence, final_response)
        results.append(cr)

    # If all pass heuristically, done
    all_met = all(r.status == "met" for r in results)
    any_evidence = len(evidence) > 0

    if all_met and any_evidence:
        return VerificationResult(
            status="pass",
            criteria_results=results,
            summary="All criteria met with evidence",
        )

    # Phase 0 Level 2: LLM second-pass for uncertain cases
    if llm_call and not all_met:
        try:
            return _llm_verify(goal, criteria, evidence, final_response, llm_call)
        except Exception as e:
            logger.warning("LLM verification failed: %s", e)

    # Fallback: check if we have any evidence at all
    if not any_evidence:
        return VerificationResult(
            status="fail_retriable",
            criteria_results=results,
            summary="No evidence collected",
            missing_evidence=[c["description"] for c in criteria],
        )

    unmet = [r for r in results if r.status == "unmet"]
    return VerificationResult(
        status="fail_retriable",
        criteria_results=results,
        summary=f"{len(unmet)} criteria unmet",
        missing_evidence=[r.description for r in unmet],
    )


def _check_criterion_heuristic(
    criterion: dict,
    evidence: list[dict],
    final_response: str,
) -> "CriterionResult":
    """Simple heuristic: does any evidence or the response mention the criterion?"""
    from brain.models import CriterionResult

    desc = criterion["description"].lower()
    key = criterion["criterion_key"]

    # Check if evidence references relate to this criterion
    evidence_texts = " ".join(
        (e.get("summary", "") + " " + str(e.get("payload_json", "")))
        for e in evidence
    ).lower()

    response_lower = final_response.lower()

    # Simple keyword overlap check
    desc_words = set(desc.split()) - {"the", "a", "is", "are", "has", "been", "已", "的", "了"}
    matched_in_evidence = sum(1 for w in desc_words if w in evidence_texts)
    matched_in_response = sum(1 for w in desc_words if w in response_lower)

    total_words = max(len(desc_words), 1)
    coverage = (matched_in_evidence + matched_in_response) / (total_words * 2)

    if coverage > 0.3 and (matched_in_evidence > 0 or len(evidence) > 0):
        return CriterionResult(
            criterion_key=key,
            description=criterion["description"],
            status="met",
            evidence_summary=f"Evidence coverage: {coverage:.0%}",
        )

    return CriterionResult(
        criterion_key=key,
        description=criterion["description"],
        status="unmet",
        evidence_summary=f"Insufficient evidence (coverage: {coverage:.0%})",
    )


def _llm_verify(goal, criteria, evidence, final_response, llm_call):
    """LLM-based verification for uncertain cases."""
    from brain.models import VerificationResult, CriterionResult

    evidence_summary = "\n".join(
        f"- [{e.get('source_type', '?')}] {e.get('summary', 'no summary')}"
        for e in evidence[:10]
    )

    user_prompt = f"""Goal: {goal}

Success Criteria:
{json.dumps([{"criterion_key": f"c{i}", **c} for i, c in enumerate(criteria)], indent=2, ensure_ascii=False)}

Evidence Collected:
{evidence_summary if evidence_summary else "(no evidence)"}

Final Response (first 2000 chars):
{final_response[:2000]}

Verify each criterion."""

    raw = llm_call(VERIFIER_SYSTEM_PROMPT, user_prompt)
    data = json.loads(raw.strip().strip("`").strip())

    results = []
    for cr in data.get("criteria_results", []):
        results.append(CriterionResult(
            criterion_key=cr.get("criterion_key", "?"),
            description=cr.get("description", ""),
            status=cr.get("status", "unmet"),
            evidence_summary=cr.get("evidence_summary", ""),
        ))

    return VerificationResult(
        status=data.get("overall_status", "fail_retriable"),
        criteria_results=results,
        summary=data.get("summary", ""),
        missing_evidence=data.get("missing_evidence", []),
    )
```

### 3.5 `brain/evidence.py` — Evidence Store

**Purpose**: Capture tool outputs and relevant data as task evidence.

```python
"""Evidence Store — capture and query task completion evidence."""

import json
import logging
import time
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


def _eid() -> str:
    return f"ev_{uuid.uuid4().hex[:12]}"


def capture_from_tool_result(
    task_id: str,
    tool_name: str,
    tool_call_id: str,
    output: str,
    db: "SessionDB",
) -> str:
    """
    Capture evidence from a tool call result.
    Returns evidence_id.
    """
    # Parse output to extract summary
    summary = _extract_summary(output, tool_name)

    eid = _eid()
    db._execute_write(
        """INSERT INTO evidence_records (id, task_id, source_type, source_ref,
           tool_name, summary, payload_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (eid, task_id, "tool_output", tool_call_id, tool_name,
         summary, output[:10000], time.time()),
    )
    return eid


def capture_from_response(
    task_id: str,
    response_text: str,
    db: "SessionDB",
) -> str:
    """Capture the final LLM response as evidence."""
    eid = _eid()
    summary = response_text[:200] if response_text else ""
    db._execute_write(
        """INSERT INTO evidence_records (id, task_id, source_type, source_ref,
           tool_name, summary, payload_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (eid, task_id, "llm_response", "", None,
         summary, json.dumps({"text": response_text[:5000]}), time.time()),
    )
    return eid


def get_evidence_for_task(task_id: str, db: "SessionDB") -> list[dict]:
    """Retrieve all evidence for a task."""
    rows = db.conn.execute(
        """SELECT id, source_type, source_ref, tool_name, summary, payload_json, created_at
           FROM evidence_records WHERE task_id = ? ORDER BY created_at""",
        (task_id,),
    ).fetchall()

    return [
        {
            "id": r[0], "source_type": r[1], "source_ref": r[2],
            "tool_name": r[3], "summary": r[4],
            "payload_json": r[5], "created_at": r[6],
        }
        for r in rows
    ]


def _extract_summary(output: str, tool_name: str) -> str:
    """Extract a short summary from tool output."""
    if not output:
        return f"{tool_name}: empty output"

    # Try to parse as JSON and get key fields
    try:
        data = json.loads(output)
        if isinstance(data, dict):
            for key in ("summary", "result", "content", "output", "message"):
                if key in data:
                    val = str(data[key])
                    return val[:200]
            return json.dumps(data, ensure_ascii=False)[:200]
    except (json.JSONDecodeError, TypeError):
        pass

    return output[:200]
```

### 3.6 `brain/task_store.py` — Task CRUD + State Machine

```python
"""Task Store — CRUD and state machine for structured tasks."""

import json
import logging
import time
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


def _tid() -> str:
    return f"task_{uuid.uuid4().hex[:12]}"


def create_task(
    db: "SessionDB",
    session_id: str,
    goal: str,
    *,
    event_text: str = "",
    task_type: str = "general",
    priority: str = "medium",
    risk_level: str = "low",
    parent_task_id: str | None = None,
    requires_approval: bool = False,
    budget_tokens: int | None = None,
    budget_ms: int | None = None,
) -> str:
    """Create a new task. Returns task_id."""
    tid = _tid()
    now = time.time()

    db._execute_write(
        """INSERT INTO tasks (id, parent_task_id, session_id, event_text, task_type,
           goal, status, priority, risk_level, requires_approval,
           budget_tokens, budget_ms, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (tid, parent_task_id, session_id, event_text, task_type,
         goal, "received", priority, risk_level, requires_approval,
         budget_tokens, budget_ms, now, now),
    )

    _log_transition(db, tid, "none", "received", "task_created")
    return tid


def update_task_status(
    db: "SessionDB",
    task_id: str,
    new_status: str,
    *,
    reason: str = "",
    plan_json: str | None = None,
    failure_reason: str | None = None,
    verification_status: str | None = None,
    verification_json: str | None = None,
) -> None:
    """Transition a task to a new status."""
    row = db.conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"Task not found: {task_id}")

    old_status = row[0]
    _validate_transition(old_status, new_status)

    updates = ["status = ?", "updated_at = ?"]
    params = [new_status, time.time()]

    if plan_json is not None:
        updates.append("plan_json = ?")
        params.append(plan_json)
    if failure_reason is not None:
        updates.append("failure_reason = ?")
        params.append(failure_reason)
    if verification_status is not None:
        updates.append("verification_status = ?")
        params.append(verification_status)
    if verification_json is not None:
        updates.append("verification_json = ?")
        params.append(verification_json)
    if new_status == "running" and old_status != "running":
        updates.append("started_at = ?")
        params.append(time.time())
    if new_status in ("completed", "failed", "cancelled"):
        updates.append("completed_at = ?")
        params.append(time.time())

    params.append(task_id)
    db._execute_write(
        f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?",
        tuple(params),
    )

    _log_transition(db, task_id, old_status, new_status, reason)


def get_task(db: "SessionDB", task_id: str) -> dict | None:
    """Get a task by ID."""
    row = db.conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if not row:
        return None
    cols = [d[0] for d in db.conn.execute("SELECT * FROM tasks LIMIT 0").description]
    return dict(zip(cols, row))


def get_active_tasks(db: "SessionDB", session_id: str) -> list[dict]:
    """Get non-terminal tasks for a session."""
    rows = db.conn.execute(
        """SELECT id, task_type, goal, status, priority, risk_level, created_at
           FROM tasks
           WHERE session_id = ? AND status NOT IN ('completed', 'failed', 'cancelled')
           ORDER BY created_at""",
        (session_id,),
    ).fetchall()
    return [
        {"id": r[0], "task_type": r[1], "goal": r[2], "status": r[3],
         "priority": r[4], "risk_level": r[5], "created_at": r[6]}
        for r in rows
    ]


def save_criteria(db: "SessionDB", task_id: str, criteria: list[str]) -> None:
    """Save success criteria for a task."""
    now = time.time()
    for i, desc in enumerate(criteria):
        db._execute_write(
            """INSERT INTO task_criteria (task_id, criterion_key, description,
               status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, f"c{i}", desc, "pending", now, now),
        )


def get_criteria(db: "SessionDB", task_id: str) -> list[dict]:
    """Get criteria for a task."""
    rows = db.conn.execute(
        """SELECT criterion_key, description, status, evidence_ids
           FROM task_criteria WHERE task_id = ? ORDER BY criterion_key""",
        (task_id,),
    ).fetchall()
    return [
        {"criterion_key": r[0], "description": r[1],
         "status": r[2], "evidence_ids": r[3]}
        for r in rows
    ]


def increment_retry(db: "SessionDB", task_id: str) -> tuple[int, int]:
    """Increment retry count. Returns (new_count, max_retries)."""
    row = db.conn.execute(
        "SELECT retry_count, max_retries FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Task not found: {task_id}")

    new_count = row[0] + 1
    db._execute_write(
        "UPDATE tasks SET retry_count = ?, updated_at = ? WHERE id = ?",
        (new_count, time.time(), task_id),
    )
    return new_count, row[1]


def _validate_transition(from_state: str, to_state: str) -> None:
    """Validate state transition."""
    ALLOWED = {
        "none": {"received"},
        "received": {"triaged", "cancelled"},
        "triaged": {"planned", "running", "cancelled"},
        "planned": {"running", "cancelled"},
        "running": {"verifying", "failed", "blocked", "cancelled"},
        "verifying": {"completed", "failed", "blocked", "running"},
        "blocked": {"running", "cancelled"},
        "completed": set(),
        "failed": {"running"},  # retry
        "cancelled": set(),
    }
    allowed = ALLOWED.get(from_state, set())
    if to_state not in allowed:
        raise ValueError(f"Invalid task transition: {from_state} -> {to_state}")


def _log_transition(db, task_id, from_state, to_state, reason):
    db._execute_write(
        """INSERT INTO task_transitions (task_id, from_state, to_state, reason, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (task_id, from_state, to_state, reason, time.time()),
    )
```

---

## 4. Surgical Insertion Points

### 4.1 Gateway Integration — `gateway/run.py`

**Insert at**: `_handle_message_with_agent()` (line 1978), between session setup
and `_run_agent()` call.

The key change: wrap the existing `_run_agent()` call with Executive triage,
and for tasks, add Planner + Verifier around it.

```python
# In _handle_message_with_agent(), AFTER session/context setup,
# BEFORE calling _run_agent():

async def _handle_message_with_agent(self, event, source, _quick_key: str):
    # ... existing session setup code (lines 1978-2108) ...

    # ═══ NEW: Executive Triage ═══════════════════════════════════
    from brain.executive import triage
    from brain import task_store

    triage_result = triage(
        event.text,
        has_media=bool(getattr(event, 'media_urls', None)),
        session_history_len=len(history),
        active_task_count=len(task_store.get_active_tasks(
            self.session_store.session_db, session_entry.session_id
        )),
    )

    if triage_result.decision == "create_task":
        return await self._run_agent_with_task(
            event, source, _quick_key,
            session_entry, context_prompt, history,
            triage_result,
        )

    # ═══ Existing path: direct reply ═════════════════════════════
    # ... existing _run_agent() call ...
```

**New method**: `_run_agent_with_task()` — orchestrates Plan → Execute → Verify:

```python
async def _run_agent_with_task(
    self, event, source, _quick_key,
    session_entry, context_prompt, history,
    triage_result,
):
    """Run agent with structured task tracking."""
    from brain import task_store, planner, verifier, evidence
    from brain.models import _id

    db = self.session_store.session_db
    session_id = session_entry.session_id

    # 1. Create task
    task_id = task_store.create_task(
        db, session_id,
        goal=event.text,
        event_text=event.text,
        task_type=triage_result.task_type,
        priority=triage_result.priority,
        risk_level=triage_result.risk_level,
        requires_approval=triage_result.requires_approval,
    )
    task_store.update_task_status(db, task_id, "triaged", reason="executive_triage")

    # 2. Generate plan (using auxiliary LLM)
    def _plan_llm_call(system, user):
        """Use auxiliary client for planning."""
        from agent.auxiliary_client import get_auxiliary_client
        client = get_auxiliary_client()
        resp = client.chat.completions.create(
            model=client._model or "gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
        )
        return resp.choices[0].message.content

    plan = planner.generate_plan(
        goal=event.text,
        task_type=triage_result.task_type,
        llm_call=_plan_llm_call,
    )
    task_store.update_task_status(
        db, task_id, "planned",
        reason="plan_generated",
        plan_json=json.dumps(plan.to_dict(), ensure_ascii=False),
    )
    task_store.save_criteria(db, task_id, plan.success_criteria)

    # 3. Inject plan context into system prompt for the agent
    plan_context = (
        f"\n\n[TASK PLAN — task_id: {task_id}]\n"
        f"Goal: {plan.goal}\n"
        f"Success criteria:\n"
        + "\n".join(f"  - {c}" for c in plan.success_criteria)
        + "\n\nYou MUST address ALL success criteria. "
        "Collect evidence for each criterion through tool usage."
    )
    enriched_prompt = context_prompt + plan_context

    # 4. Run agent (existing path)
    task_store.update_task_status(db, task_id, "running", reason="execution_started")

    result = await self._run_agent(
        event.text, enriched_prompt, history,
        source, session_id,
        session_key=session_entry.session_key,
        event_message_id=getattr(event, 'message_id', None),
    )

    # 5. Capture evidence from tool calls in the result
    for msg in result.get("messages", []):
        if msg.get("role") in ("tool", "function") and msg.get("content"):
            evidence.capture_from_tool_result(
                task_id,
                tool_name=msg.get("tool_name", msg.get("name", "unknown")),
                tool_call_id=msg.get("tool_call_id", ""),
                output=msg.get("content", ""),
                db=db,
            )

    final_response = result.get("final_response", "")
    if final_response:
        evidence.capture_from_response(task_id, final_response, db)

    # 6. Verify
    task_store.update_task_status(db, task_id, "verifying", reason="verification_started")

    criteria = task_store.get_criteria(db, task_id)
    ev_records = evidence.get_evidence_for_task(task_id, db)

    vr = verifier.verify_task(
        goal=plan.goal,
        criteria=criteria,
        evidence=ev_records,
        final_response=final_response,
    )

    # 7. Handle verification result
    task_store.update_task_status(
        db, task_id,
        "completed" if vr.status == "pass" else "failed",
        reason=f"verification_{vr.status}",
        verification_status=vr.status,
        verification_json=json.dumps(vr.to_dict(), ensure_ascii=False),
    )

    if vr.status == "fail_retriable":
        retry_count, max_retries = task_store.increment_retry(db, task_id)
        if retry_count <= max_retries:
            # Retry: re-run with feedback
            task_store.update_task_status(db, task_id, "running", reason="retry")
            retry_prompt = (
                f"\n\n[RETRY — attempt {retry_count + 1}]\n"
                f"Previous attempt failed verification:\n"
                f"Missing: {', '.join(vr.missing_evidence)}\n"
                f"Please address the missing criteria."
            )
            result = await self._run_agent(
                event.text + retry_prompt,
                enriched_prompt, history,
                source, session_id,
            )
            # Re-verify after retry (simplified — one retry only in Phase 0)
            final_response = result.get("final_response", final_response)
            task_store.update_task_status(
                db, task_id, "completed",
                reason="retry_completed",
            )

    return result
```

### 4.2 Schema Migration — `hermes_state.py`

**Insert at**: After existing `SCHEMA_SQL` and `FTS_SQL` constants.

```python
# Add to hermes_state.py

SCHEMA_V7_SQL = """
-- AgentEOS Phase 0: Task tracking tables

CREATE TABLE IF NOT EXISTS tasks ( ... );  -- as defined in section 2.1
CREATE TABLE IF NOT EXISTS task_criteria ( ... );  -- section 2.2
CREATE TABLE IF NOT EXISTS evidence_records ( ... );  -- section 2.3
CREATE TABLE IF NOT EXISTS task_transitions ( ... );  -- section 2.4
CREATE TABLE IF NOT EXISTS policy_evaluations ( ... );  -- section 2.5
"""

# In SessionDB.__init__, add migration:
def _migrate_v7(self):
    """Add AgentEOS brain tables."""
    try:
        self.conn.executescript(SCHEMA_V7_SQL)
        self.conn.execute(
            "UPDATE schema_version SET version = 7"
        )
        self.conn.commit()
        logger.info("Migrated state.db to schema v7 (AgentEOS brain)")
    except Exception as e:
        logger.error("Schema v7 migration failed: %s", e)
        raise
```

### 4.3 Task State Extension — `agent/task_state.py`

**Extend** the existing state machine with plan/verify states:

```python
# Add to TASK_STATES:
TASK_STATES_EXTENDED = (
    "received", "triaged", "planned", "running",
    "waiting_model", "waiting_tool", "verifying", "retrying",
    "completed", "failed", "blocked", "cancelled",
)

# Add to ALLOWED_TRANSITIONS:
ALLOWED_TRANSITIONS_EXTENDED = {
    "received": {"triaged", "cancelled"},
    "triaged": {"planned", "running", "cancelled"},
    "planned": {"running", "cancelled"},
    "running": {"waiting_model", "waiting_tool", "verifying",
                "retrying", "completed", "failed", "blocked", "cancelled"},
    # ... existing + new states
}
```

---

## 5. Directory Structure

```
~/.hermes/hermes-agent/
├── brain/                    # ← NEW: AgentEOS brain modules
│   ├── __init__.py
│   ├── models.py             # Shared dataclasses
│   ├── executive.py          # Event triage
│   ├── planner.py            # Structured plan generation
│   ├── verifier.py           # Task verification
│   ├── evidence.py           # Evidence store
│   ├── task_store.py         # Task CRUD + state machine
│   ├── policy.py             # Unified policy eval (Phase 0.5)
│   └── world_state.py        # Task-centric world state (Phase 0.5)
├── gateway/
│   └── run.py                # Modified: insert Executive triage
├── agent/
│   └── task_state.py         # Modified: extend states
├── hermes_state.py           # Modified: add tables + migration
└── ... (everything else unchanged)
```

---

## 6. Data Flow: Task Path (End-to-End)

```
1. User: "幫我研究三個 Python ORM 框架的優缺點"
         ↓
2. Gateway._handle_message() → _handle_message_with_agent()
         ↓
3. Executive.triage()
   → decision: "create_task"
   → task_type: "research"
   → risk: "low"
         ↓
4. task_store.create_task() → task_id: "task_abc123"
   → status: received → triaged
         ↓
5. Planner.generate_plan()
   → goal: "研究三個 Python ORM 框架的優缺點"
   → success_criteria:
     - "已列出至少 3 個 ORM 框架"
     - "每個框架有優點和缺點"
     - "有引用來源或技術依據"
   → subtasks: [s1: web_search, s2: extract, s3: compare]
   → status: planned
         ↓
6. Inject plan into system prompt
   → "You MUST address ALL success criteria..."
         ↓
7. _run_agent() → existing LLM loop with tools
   → tool calls: web_search, web_extract, ...
   → status: running
         ↓
8. Evidence capture (from tool results in messages)
   → evidence_records: [ev_001, ev_002, ev_003, ...]
         ↓
9. Verifier.verify_task()
   → check criteria against evidence
   → status: verifying → completed (if pass)
         ↓
10. Response sent to user (same as before)
    + task marked completed in DB
    + evidence chain preserved
```

---

## 7. Sprint Plan

### Sprint 0.1 (Week 1-2): Foundation
- [ ] Create `brain/` directory + `models.py`
- [ ] Create `brain/task_store.py` with CRUD + state machine
- [ ] Add schema v7 migration to `hermes_state.py`
- [ ] Create `brain/evidence.py`
- [ ] Write unit tests for task_store + evidence

### Sprint 0.2 (Week 2-3): Executive + Planner
- [ ] Create `brain/executive.py` with heuristic triage
- [ ] Create `brain/planner.py` with LLM-based planning
- [ ] Insert Executive triage into `gateway/run.py`
- [ ] Add `_run_agent_with_task()` to gateway
- [ ] Test: message triage correctly splits direct/task paths

### Sprint 0.3 (Week 3-4): Verifier + Evidence Pipeline
- [ ] Create `brain/verifier.py`
- [ ] Wire evidence capture into `_run_agent_with_task()`
- [ ] Wire verifier after agent completion
- [ ] Implement retry on fail_retriable
- [ ] Test: end-to-end task flow with verification

### Sprint 0.4 (Week 4-5): Hardening
- [ ] CLI commands: `/tasks` (list active), `/task <id>` (detail)
- [ ] Basic metrics: task success/fail counts in `/status`
- [ ] Graceful fallback: if brain modules fail, fall through to direct path
- [ ] Edge cases: interrupted tasks, timeout, empty responses
- [ ] Integration test with all 3 use cases

---

## 8. Risk Mitigation

### "Brain modules crash"
**Mitigation**: Entire brain path wrapped in try/except. On failure,
fall through to existing direct-reply path. User sees no difference,
just loses task tracking for that message.

```python
try:
    return await self._run_agent_with_task(...)
except Exception as e:
    logger.error("Brain task path failed, falling back to direct: %s", e)
    # Fall through to existing _run_agent() path
```

### "Planner generates bad plans"
**Mitigation**: `_fallback_plan()` always works. Max subtasks capped at 8.
Max criteria capped at 5. Invalid JSON falls back to fallback plan.

### "Verifier is too strict / too lenient"
**Mitigation**: Phase 0 verifier is opt-in soft. Failed verification
doesn't block the response from being sent. It just logs the result.
Strictness tuning happens in Phase 1.

### "Performance overhead"
**Mitigation**: Executive triage is pure heuristics (~0ms).
Planner uses auxiliary (cheap) model. Verifier heuristic pass is ~0ms.
LLM verification only runs on uncertain cases.

### "SQLite contention"
**Mitigation**: All new writes use existing `_execute_write()` with
WAL mode + retry + jitter. Same pattern as existing messages table.

---

## 9. Success Criteria for Phase 0

Phase 0 is done when:

1. ✅ Messages are triaged into direct-reply vs task paths
2. ✅ Tasks are persisted in `tasks` table with full lifecycle
3. ✅ Each task has structured success criteria
4. ✅ Tool outputs are captured as evidence
5. ✅ Completed tasks have verification results
6. ✅ Failed tasks have failure reasons
7. ✅ Task state transitions are logged
8. ✅ Brain failure falls through gracefully to direct path
9. ✅ `/tasks` shows active task list
10. ✅ All 3 use cases work: reply, summary, research/coding

---

## 10. What Phase 0 Enables for v1

After Phase 0, the following v1 modules become straightforward to add:

| v1 Module | Phase 0 Foundation |
|---|---|
| World Model Service | Tasks table + evidence → add `world_state` queries |
| Policy Service | `policy_evaluations` table → unify existing approval |
| Observability | `task_transitions` + evidence → add metrics endpoints |
| Task Recovery | Tasks in DB → add `/resume` by task_id |
| Identity Core | Add `identity_profiles` table + prompt injection |

Phase 0 turns Hermes from "LLM loop with tools" into
"LLM loop with tools **plus task tracking, planning, and verification**."
That's the minimal brain that makes everything else possible.
