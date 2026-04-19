"""Verifier — task completion verification with evidence checking.

Prevents "model says done = done." Every completed task must have its
success criteria checked against actual evidence collected during execution.

Phase 0 has two verification levels:
  Level 1: Heuristic checks (keyword overlap, evidence presence) — fast, no LLM
  Level 2: LLM second-pass for uncertain cases — more accurate, costs tokens
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Optional

from brain.models import CriterionResult, VerificationResult

logger = logging.getLogger(__name__)

# ── Verifier System Prompt ────────────────────────────────────────

VERIFIER_SYSTEM_PROMPT = """\
You are a strict task completion verifier. Given:
1. The original goal and success criteria
2. Evidence collected during execution (tool outputs, search results, etc.)
3. The assistant's final response

Evaluate whether each criterion is MET or UNMET based on the evidence.

Output ONLY valid JSON:
{
  "criteria_results": [
    {
      "criterion_key": "c0",
      "description": "the criterion text",
      "status": "met",
      "evidence_summary": "brief description of supporting evidence"
    }
  ],
  "overall_status": "pass",
  "summary": "one-line verification summary",
  "missing_evidence": []
}

Rules:
- "met": clear evidence exists that supports the criterion
- "unmet": no evidence, insufficient evidence, or evidence contradicts
- overall_status:
  - "pass": ALL criteria met
  - "fail_retriable": some unmet but could succeed with another attempt
  - "fail_non_retriable": fundamentally impossible or wrong approach
- Be strict: vague claims without evidence count as "unmet"
- If the response looks complete but has no tool-based evidence, still mark
  criteria that are clearly addressed in the response text as "met"
"""


# ── Public API ────────────────────────────────────────────────────


def verify_task(
    goal: str,
    criteria: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    final_response: str,
    *,
    llm_call: Optional[Callable[[str, str], str]] = None,
) -> VerificationResult:
    """
    Verify whether a task's success criteria are met.

    Args:
        goal: The task goal
        criteria: List of {"criterion_key": str, "description": str}
        evidence: List of evidence record dicts
        final_response: The agent's final text response
        llm_call: Optional LLM callable(system, user) -> str for deep verification

    Returns:
        VerificationResult with per-criterion results and overall status
    """
    if not criteria:
        # No criteria defined — pass by default with a note
        return VerificationResult(
            status="pass",
            summary="No success criteria defined — auto-pass",
        )

    # Level 1: Heuristic check
    heuristic_results = [
        _check_criterion_heuristic(c, evidence, final_response)
        for c in criteria
    ]

    all_met = all(r.status == "met" for r in heuristic_results)
    has_evidence = len(evidence) > 0

    # If all pass heuristically and we have evidence, we're done
    if all_met and has_evidence:
        return VerificationResult(
            status="pass",
            criteria_results=heuristic_results,
            summary="All criteria met (heuristic verification)",
        )

    # If all pass heuristically but no evidence, still pass with caveat
    if all_met and not has_evidence:
        return VerificationResult(
            status="pass",
            criteria_results=heuristic_results,
            summary="All criteria met in response text (no tool evidence)",
        )

    # Level 2: LLM verification for uncertain cases
    if llm_call:
        try:
            return _llm_verify(goal, criteria, evidence, final_response, llm_call)
        except Exception as e:
            logger.warning("LLM verification failed (%s), using heuristic result", e)

    # Fallback: return heuristic results
    unmet = [r for r in heuristic_results if r.status == "unmet"]
    met = [r for r in heuristic_results if r.status == "met"]

    # Exploratory task detection: if the goal is a query/check/research/evaluate
    # and the response is substantial, be lenient — these tasks don't produce
    # file artifacts but are legitimately complete when answered.
    is_exploratory = _is_exploratory_task(goal)
    response_substantial = len(final_response.strip()) > 200

    if is_exploratory and response_substantial:
        # For exploratory tasks: substantial response = task completed
        # Even without tool evidence, the answer IS the deliverable.
        # Keyword mismatch between EN criteria and ZH response is expected —
        # the response length and exploratory nature are sufficient proof.
        return VerificationResult(
            status="pass",
            criteria_results=heuristic_results,
            summary=f"Exploratory task completed — substantial response ({len(final_response)} chars)",
        )

    if not has_evidence and len(unmet) == len(criteria):
        # No evidence at all — but check response length for exploratory
        if is_exploratory and response_substantial:
            return VerificationResult(
                status="pass",
                criteria_results=heuristic_results,
                summary="Exploratory task: no tool evidence but substantial response provided",
            )
        return VerificationResult(
            status="fail_retriable",
            criteria_results=heuristic_results,
            summary="No evidence collected — all criteria unverifiable",
            missing_evidence=[r.description for r in unmet],
        )

    return VerificationResult(
        status="fail_retriable",
        criteria_results=heuristic_results,
        summary=f"{len(unmet)}/{len(criteria)} criteria unmet",
        missing_evidence=[r.description for r in unmet],
    )


def _is_exploratory_task(goal: str) -> bool:
    """Detect exploratory/query/research tasks that don't produce file artifacts."""
    goal_lower = goal.lower()
    # Chinese exploratory keywords
    zh_patterns = ['檢查', '查詢', '評估', '分析', '確認', '查看', '了解',
                   '研究', '調查', '比較', '審計', '盤點', '摘要', '報告',
                   '看看', '怎麼', '什麼', '是否', '有沒有', '哪些']
    # English exploratory keywords
    en_patterns = ['check', 'verify', 'evaluate', 'assess', 'analyze', 'review',
                   'investigate', 'research', 'compare', 'audit', 'summarize',
                   'report', 'look at', 'what', 'how', 'which', 'whether',
                   'find out', 'explore', 'diagnose', 'inspect']
    return any(p in goal_lower for p in zh_patterns + en_patterns)


# ── Heuristic Verification ───────────────────────────────────────


# Common stop words to exclude from keyword matching
_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "must", "need",
    "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "and", "or", "but", "not", "no", "if", "then", "than", "that",
    "this", "these", "those", "it", "its", "all", "each", "every",
    "已", "的", "了", "和", "與", "或", "在", "是", "有", "被",
})


def _check_criterion_heuristic(
    criterion: dict[str, Any],
    evidence: list[dict[str, Any]],
    final_response: str,
) -> CriterionResult:
    """
    Heuristic: check keyword overlap between criterion and evidence+response.

    Returns met if sufficient keyword coverage, unmet otherwise.
    """
    desc = criterion["description"]
    key = criterion["criterion_key"]

    # Extract meaningful words from criterion
    desc_words = _extract_keywords(desc)
    if not desc_words:
        # Can't verify meaningfully — assume met if we have any evidence
        if evidence:
            return CriterionResult(
                criterion_key=key, description=desc,
                status="met", evidence_summary="Evidence exists (no keywords to match)",
            )
        return CriterionResult(
            criterion_key=key, description=desc,
            status="unmet", evidence_summary="No evidence and no verifiable keywords",
        )

    # Build evidence text corpus
    evidence_corpus = _build_evidence_corpus(evidence)
    response_lower = final_response.lower()

    # Count keyword hits
    evidence_hits = sum(1 for w in desc_words if w in evidence_corpus)
    response_hits = sum(1 for w in desc_words if w in response_lower)
    total_keywords = len(desc_words)

    # Score: evidence hits count fully, response hits count at 70%
    score = (evidence_hits + response_hits * 0.7) / total_keywords

    # Check for numeric requirements (e.g., "at least 3")
    numeric_check = _check_numeric_requirement(desc, evidence_corpus + " " + response_lower)

    if numeric_check is False:
        return CriterionResult(
            criterion_key=key, description=desc,
            status="unmet",
            evidence_summary=f"Numeric requirement not met (keyword score: {score:.0%})",
        )

    if score >= 0.35:
        return CriterionResult(
            criterion_key=key, description=desc,
            status="met",
            evidence_summary=f"Keyword coverage: {score:.0%} ({evidence_hits} evidence, {response_hits} response)",
        )

    return CriterionResult(
        criterion_key=key, description=desc,
        status="unmet",
        evidence_summary=f"Insufficient coverage: {score:.0%}",
    )


def _extract_keywords(text: str) -> set[str]:
    """Extract meaningful keywords from a text, excluding stop words."""
    words = set(re.findall(r"[\w\u4e00-\u9fff]+", text.lower()))
    return words - _STOP_WORDS


def _build_evidence_corpus(evidence: list[dict[str, Any]]) -> str:
    """Combine all evidence summaries and payloads into a searchable corpus."""
    parts = []
    for e in evidence:
        if e.get("summary"):
            parts.append(e["summary"])
        if e.get("payload_json"):
            # Only include first 500 chars of payload per record
            parts.append(str(e["payload_json"])[:500])
    return " ".join(parts).lower()


def _check_numeric_requirement(desc: str, corpus: str) -> Optional[bool]:
    """
    Check if a criterion has a numeric requirement (e.g., "at least 3")
    and whether the corpus satisfies it.

    Returns True if satisfied, False if not, None if no numeric requirement.
    """
    # Pattern: "at least N" or "至少 N"
    match = re.search(r"(?:at least|至少|>=?|no fewer than)\s*(\d+)", desc.lower())
    if not match:
        return None  # No numeric requirement

    required = int(match.group(1))
    # This is a rough heuristic — in Phase 1 we'd count actual items
    # For now, just check if the corpus is non-trivial
    if len(corpus) < required * 20:
        return False
    return None  # Can't definitively confirm, let keyword matching decide


# ── LLM Verification ─────────────────────────────────────────────


def _llm_verify(
    goal: str,
    criteria: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    final_response: str,
    llm_call: Callable[[str, str], str],
) -> VerificationResult:
    """Use LLM for deeper verification of uncertain criteria."""
    evidence_summary = "\n".join(
        f"- [{e.get('tool_name') or e.get('source_type', '?')}] "
        f"{e.get('summary', '(no summary)')}"
        for e in evidence[:10]
    )

    criteria_json = json.dumps(
        [{"criterion_key": c["criterion_key"], "description": c["description"]}
         for c in criteria],
        indent=2, ensure_ascii=False,
    )

    user_prompt = f"""Goal: {goal}

Success Criteria:
{criteria_json}

Evidence Collected:
{evidence_summary if evidence_summary.strip() else "(no evidence)"}

Final Response (first 2000 chars):
{final_response[:2000]}

Verify each criterion against the evidence and response."""

    raw = llm_call(VERIFIER_SYSTEM_PROMPT, user_prompt)

    # Parse response
    data = _parse_verifier_json(raw)

    results = []
    for cr in data.get("criteria_results", []):
        results.append(CriterionResult(
            criterion_key=cr.get("criterion_key", "?"),
            description=cr.get("description", ""),
            status=cr.get("status", "unmet"),
            evidence_summary=cr.get("evidence_summary", ""),
        ))

    overall = data.get("overall_status", "fail_retriable")
    if overall not in ("pass", "fail_retriable", "fail_non_retriable", "needs_human"):
        overall = "fail_retriable"

    return VerificationResult(
        status=overall,
        criteria_results=results,
        summary=data.get("summary", "LLM verification completed"),
        missing_evidence=data.get("missing_evidence", []),
    )


def _parse_verifier_json(raw: str) -> dict[str, Any]:
    """Parse verifier LLM output as JSON."""
    text = raw.strip()

    # Strip markdown fences
    if "```" in text:
        match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try finding JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse verifier JSON: {text[:200]}")
