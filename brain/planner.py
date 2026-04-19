"""Planner Engine — structured task graph generation.

Takes a task goal and produces a PlanSpec with success criteria, subtasks,
risks, and recommended tools. Uses LLM with constrained JSON output.

Phase 0: LLM-based planning with fallback to minimal heuristic plans.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Optional

from brain.models import PlanSpec, SubtaskSpec

logger = logging.getLogger(__name__)

# ── Constraints ───────────────────────────────────────────────────

MAX_SUBTASKS = 8
MAX_CRITERIA = 5
MAX_RISKS = 3

# ── Planner System Prompt ─────────────────────────────────────────

PLANNER_SYSTEM_PROMPT = """\
You are a task planner. Given a user's goal, produce a structured execution plan.

Output ONLY valid JSON matching this schema:
{
  "goal": "restate the goal clearly and concisely",
  "success_criteria": ["criterion 1", "criterion 2"],
  "subtasks": [
    {"id": "s1", "description": "...", "tool": "tool_name_or_null", "depends_on": []},
    {"id": "s2", "description": "...", "tool": null, "depends_on": ["s1"]}
  ],
  "risks": ["risk 1"],
  "recommended_tools": ["tool1", "tool2"]
}

Rules:
- success_criteria: 2-5 concrete, objectively verifiable conditions.
  Each must be checkable by looking at outputs/evidence (not subjective).
- subtasks: 1-8 steps maximum. Keep it minimal — don't over-decompose.
- tool: name of a tool if applicable, or null for LLM-only steps.
- depends_on: list of subtask IDs that must complete before this one.
- risks: 0-3 potential failure points.
- recommended_tools: tools that would help complete the task.

Good criteria examples:
- "At least 3 ORM frameworks listed with names and descriptions"
- "Each framework has at least 2 pros and 2 cons"
- "Output contains code examples for each framework"

Bad criteria examples (too vague):
- "Good quality output"
- "Task completed successfully"
"""


# ── Public API ────────────────────────────────────────────────────


def generate_plan(
    goal: str,
    *,
    task_type: str = "general",
    available_tools: Optional[list[str]] = None,
    context: str = "",
    llm_call: Optional[Callable[[str, str], str]] = None,
) -> PlanSpec:
    """
    Generate a structured plan for a task.

    Args:
        goal: The user's stated goal
        task_type: research | coding | summary | general
        available_tools: List of available tool names
        context: Additional context (world state, memory excerpts)
        llm_call: Callable(system_prompt, user_message) -> str

    Returns:
        PlanSpec with goal, criteria, subtasks, risks
    """
    if llm_call is None:
        logger.debug("No LLM available for planning, using fallback")
        return _fallback_plan(goal, task_type)

    user_prompt = _build_user_prompt(goal, task_type, available_tools, context)

    try:
        raw = llm_call(PLANNER_SYSTEM_PROMPT, user_prompt)
        plan_data = _parse_plan_json(raw)
        plan = _validate_and_cap(plan_data, goal)
        logger.info(
            "Plan generated: %d criteria, %d subtasks, %d risks",
            len(plan.success_criteria), len(plan.subtasks), len(plan.risks),
        )
        return plan
    except Exception as e:
        logger.warning("Planner LLM failed (%s), using fallback plan", e)
        return _fallback_plan(goal, task_type)


# ── Prompt Construction ───────────────────────────────────────────


def _build_user_prompt(
    goal: str,
    task_type: str,
    available_tools: Optional[list[str]],
    context: str,
) -> str:
    parts = [f"Task type: {task_type}", f"Goal: {goal}"]

    if available_tools:
        # Show top 25 tools to keep prompt manageable
        tools_str = ", ".join(available_tools[:25])
        parts.append(f"Available tools: {tools_str}")

    if context:
        # Truncate context to keep prompt focused
        parts.append(f"Context: {context[:1000]}")

    parts.append("\nGenerate the plan as JSON.")
    return "\n".join(parts)


# ── JSON Parsing ──────────────────────────────────────────────────


def _parse_plan_json(raw: str) -> dict[str, Any]:
    """Extract JSON from LLM response, handling markdown fences."""
    text = raw.strip()

    # Strip markdown code fences
    if "```" in text:
        # Find content between first ``` and last ```
        match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try finding first { to last }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse plan JSON from LLM output: {text[:200]}")


# ── Validation ────────────────────────────────────────────────────


def _validate_and_cap(data: dict[str, Any], original_goal: str) -> PlanSpec:
    """Validate plan data and enforce safety caps."""
    goal = data.get("goal") or original_goal

    # Criteria: enforce 1-5
    criteria = data.get("success_criteria", [])
    if isinstance(criteria, list):
        criteria = [str(c) for c in criteria if c][:MAX_CRITERIA]
    else:
        criteria = []
    if not criteria:
        criteria = [f"Task completed: {goal[:100]}"]

    # Subtasks: enforce 1-8
    subtasks_raw = data.get("subtasks", [])
    if not isinstance(subtasks_raw, list):
        subtasks_raw = []
    subtasks_raw = subtasks_raw[:MAX_SUBTASKS]

    subtasks = []
    seen_ids = set()
    for i, s in enumerate(subtasks_raw):
        if not isinstance(s, dict):
            continue
        sid = s.get("id") or f"s{i + 1}"
        # Deduplicate IDs
        if sid in seen_ids:
            sid = f"s{i + 1}"
        seen_ids.add(sid)

        subtasks.append(SubtaskSpec(
            id=sid,
            description=str(s.get("description", f"Step {i + 1}")),
            tool=s.get("tool") if isinstance(s.get("tool"), str) else None,
            depends_on=[
                d for d in (s.get("depends_on") or [])
                if isinstance(d, str)
            ],
        ))

    if not subtasks:
        subtasks = [SubtaskSpec(id="s1", description=goal[:200])]

    # Risks: cap at 3
    risks = data.get("risks", [])
    if isinstance(risks, list):
        risks = [str(r) for r in risks if r][:MAX_RISKS]
    else:
        risks = []

    # Recommended tools
    tools = data.get("recommended_tools", [])
    if isinstance(tools, list):
        tools = [str(t) for t in tools if t]
    else:
        tools = []

    return PlanSpec(
        goal=goal,
        success_criteria=criteria,
        subtasks=subtasks,
        risks=risks,
        recommended_tools=tools,
    )


# ── Fallback Plans ────────────────────────────────────────────────


def _fallback_plan(goal: str, task_type: str) -> PlanSpec:
    """Generate a minimal plan without LLM — always works."""
    criteria, subtasks = _type_specific_defaults(goal, task_type)

    return PlanSpec(
        goal=goal,
        success_criteria=criteria,
        subtasks=subtasks,
        risks=[],
        recommended_tools=[],
    )


def _type_specific_defaults(
    goal: str,
    task_type: str,
) -> tuple[list[str], list[SubtaskSpec]]:
    """Generate type-specific default criteria and subtasks."""
    if task_type == "research":
        return (
            [
                f"Information about: {goal[:60]}",
                "分析結果或結論已在回覆中呈現 / Analysis provided in response",
            ],
            [
                SubtaskSpec(id="s1", description="Search for relevant information", tool="web_search"),
                SubtaskSpec(id="s2", description="Analyze and synthesize findings", depends_on=["s1"]),
                SubtaskSpec(id="s3", description="Compose structured summary", depends_on=["s2"]),
            ],
        )

    if task_type == "coding":
        return (
            [
                "Code changes implemented correctly",
                "No errors in execution or tests",
            ],
            [
                SubtaskSpec(id="s1", description="Read and understand existing code", tool="read_file"),
                SubtaskSpec(id="s2", description="Implement changes", tool="write_file", depends_on=["s1"]),
                SubtaskSpec(id="s3", description="Verify changes work", tool="terminal", depends_on=["s2"]),
            ],
        )

    if task_type == "summary":
        return (
            [
                "Source data retrieved successfully",
                "Summary covers all key points",
            ],
            [
                SubtaskSpec(id="s1", description="Gather source data"),
                SubtaskSpec(id="s2", description="Generate structured summary", depends_on=["s1"]),
            ],
        )

    # Check if this is an exploratory/query task — needs flexible criteria
    goal_lower = goal.lower()
    exploratory_zh = ['檢查', '查詢', '評估', '分析', '確認', '查看', '了解',
                      '研究', '調查', '比較', '審計', '盤點', '看看', '怎麼',
                      '什麼', '是否', '有沒有', '哪些']
    exploratory_en = ['check', 'verify', 'evaluate', 'assess', 'analyze', 'review',
                      'investigate', 'compare', 'audit', 'summarize', 'what',
                      'how', 'which', 'whether', 'find out', 'explore',
                      'diagnose', 'inspect', 'look at']
    is_exploratory = any(p in goal_lower for p in exploratory_zh + exploratory_en)

    if is_exploratory:
        return (
            [
                f"Question addressed: {goal[:80]}",
                "Response contains relevant information or findings",
            ],
            [
                SubtaskSpec(id="s1", description="Gather relevant information"),
                SubtaskSpec(id="s2", description="Provide clear answer or analysis", depends_on=["s1"]),
            ],
        )

    # General fallback
    return (
        [f"Task completed: {goal[:100]}"],
        [SubtaskSpec(id="s1", description=goal[:200])],
    )
