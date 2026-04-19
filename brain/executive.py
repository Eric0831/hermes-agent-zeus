"""Executive Controller — event triage and task lifecycle decisions.

The "prefrontal cortex" of AgentEOS. Decides whether an incoming message
should be handled as a direct LLM reply or routed through the structured
task pipeline (Plan → Execute → Verify).

Phase 0 uses heuristics only. Phase 1 can add LLM-based intent classification.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from brain.models import TriageResult

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────

# Messages shorter than this (without task keywords) go direct
SHORT_MSG_THRESHOLD = 120

# Task-indicating keywords (lowercase). Checked with word-boundary-aware matching.
_TASK_KEYWORDS_EN = [
    "research", "compare", "analyze", "analyse", "investigate", "find all",
    "write a", "create a", "build", "implement", "fix", "debug",
    "summarize", "review", "check all", "test", "deploy", "refactor",
    "set up", "configure", "migrate", "optimize", "benchmark",
    "generate", "scrape", "extract", "compile", "audit",
]

_TASK_KEYWORDS_ZH = [
    "整理", "分析", "比較", "研究", "調查", "寫", "建立", "修改",
    "測試", "部署", "重構", "設定", "搜尋", "查", "產生", "擷取",
    "摘要", "彙整", "評估", "修復", "優化", "爬",
]

# High-risk action indicators
_HIGH_RISK_WORDS = [
    "deploy", "delete", "drop", "remove", "kill", "shutdown",
    "部署", "刪除", "移除", "關閉", "停止",
    "push to prod", "force push", "rm -rf",
]

_MEDIUM_RISK_WORDS = [
    "send", "post", "publish", "write to", "modify", "update",
    "發送", "發布", "寫入", "修改", "更新",
]

# Simple question patterns — these bypass task creation
_QUESTION_STARTERS = [
    r"^what\s+(is|are|was|were|does|do)\b",
    r"^who\s+(is|are|was)\b",
    r"^how\s+(does|do|is|are|can|should)\b",
    r"^when\s+(is|was|did|does|will)\b",
    r"^where\s+(is|are|was|can)\b",
    r"^why\s+(is|are|does|do|did)\b",
    r"^can\s+you\s+(explain|tell|describe)\b",
    r"^(explain|describe|define)\s+",
    r"^什麼是",
    r"^怎麼",
    r"^為什麼",
    r"^哪",
    r"^誰是",
]
_QUESTION_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _QUESTION_STARTERS]

# Multi-step indicators — phrases suggesting the request needs structured work
_MULTISTEP_INDICATORS = [
    r"\band\s+then\b",
    r"\bstep\s*\d",
    r"\bfirst\s*[,.]",
    r"\b\d+\)\s",
    r"\bthen\b.*\bthen\b",
    r"然後.*然後",
    r"第[一二三四五]",
    r"步驟",
    r"先.*再.*最後",
]
_MULTISTEP_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _MULTISTEP_INDICATORS]


# ── Public API ────────────────────────────────────────────────────


def triage(
    message: str,
    *,
    has_media: bool = False,
    session_history_len: int = 0,
    active_task_count: int = 0,
) -> TriageResult:
    """
    Decide: direct reply or create structured task?

    Args:
        message: The user's message text
        has_media: Whether the message includes images/files
        session_history_len: Number of messages in current session
        active_task_count: Number of currently active (non-terminal) tasks

    Returns:
        TriageResult with decision and metadata
    """
    text = message.strip()
    text_lower = text.lower()
    text_len = len(text)

    # ── Rule 1: Empty or trivial ──
    if text_len < 5:
        return TriageResult(
            decision="direct_reply",
            reason="trivial_input",
            task_type="direct_reply",
        )

    # ── Rule 2: Slash commands should never create tasks ──
    if text.startswith("/"):
        return TriageResult(
            decision="direct_reply",
            reason="slash_command",
            task_type="direct_reply",
        )

    # ── Rule 3: Simple questions without task intent ──
    has_task = _has_task_intent(text_lower)
    if _is_simple_question(text_lower) and not has_task and text_len < 300:
        return TriageResult(
            decision="direct_reply",
            reason="simple_question",
            task_type="direct_reply",
        )

    # ── Rule 4: Short messages without task keywords ──
    if text_len < SHORT_MSG_THRESHOLD and not has_task and not has_media:
        return TriageResult(
            decision="direct_reply",
            reason="short_no_task_intent",
            task_type="direct_reply",
        )

    # ── Rule 5: Explicit task intent detected ──
    if has_task:
        task_type = _classify_task_type(text_lower)
        risk = _estimate_risk(text_lower)
        return TriageResult(
            decision="create_task",
            reason="task_intent_detected",
            task_type=task_type,
            priority=_estimate_priority(text_lower, risk),
            risk_level=risk,
            requires_approval=(risk == "high"),
        )

    # ── Rule 6: Multi-step indicators ──
    if _has_multistep_indicators(text_lower):
        return TriageResult(
            decision="create_task",
            reason="multistep_detected",
            task_type=_classify_task_type(text_lower),
            priority="medium",
            risk_level=_estimate_risk(text_lower),
        )

    # ── Rule 7: Long or complex input ──
    if text_len > 500 or has_media:
        return TriageResult(
            decision="create_task",
            reason="complex_input",
            task_type=_classify_task_type(text_lower),
            priority="medium",
            risk_level=_estimate_risk(text_lower),
        )

    # ── Default: direct reply (conservative) ──
    return TriageResult(
        decision="direct_reply",
        reason="default_direct",
        task_type="direct_reply",
    )


# ── Internal Helpers ──────────────────────────────────────────────


def _has_task_intent(text_lower: str) -> bool:
    """Check if the message contains task-indicating keywords.

    Uses word-boundary matching for English keywords to avoid false positives
    (e.g. "detest" should NOT match "test").  Chinese keywords use simple
    substring matching since Chinese has no word boundaries.
    """
    for kw in _TASK_KEYWORDS_EN:
        if re.search(r'\b' + re.escape(kw) + r'\b', text_lower):
            return True
    for kw in _TASK_KEYWORDS_ZH:
        if kw in text_lower:
            return True
    return False


def _is_simple_question(text_lower: str) -> bool:
    """Check if the message is a simple factual question."""
    if "?" not in text_lower and "？" not in text_lower:
        return False
    return any(p.search(text_lower) for p in _QUESTION_PATTERNS)


def _has_multistep_indicators(text_lower: str) -> bool:
    """Check if the message describes a multi-step process."""
    return any(p.search(text_lower) for p in _MULTISTEP_PATTERNS)


def _word_match(text: str, keyword: str) -> bool:
    """Match keyword with word boundaries for English, substring for Chinese."""
    # Chinese characters (CJK Unified Ideographs range)
    if any('\u4e00' <= c <= '\u9fff' for c in keyword):
        return keyword in text
    return bool(re.search(r'\b' + re.escape(keyword) + r'\b', text))


def _classify_task_type(text_lower: str) -> str:
    """Classify the task into a type category."""
    research_words = [
        "research", "compare", "investigate", "analyze", "analyse",
        "分析", "比較", "研究", "調查", "評估",
    ]
    if any(_word_match(text_lower, w) for w in research_words):
        return "research"

    coding_words = [
        "code", "fix", "debug", "implement", "build", "refactor",
        "test", "deploy", "migrate",
        "修改", "修復", "寫程式", "重構", "測試", "部署",
    ]
    if any(_word_match(text_lower, w) for w in coding_words):
        return "coding"

    summary_words = [
        "summarize", "summary", "digest", "recap",
        "整理", "摘要", "彙整",
    ]
    if any(_word_match(text_lower, w) for w in summary_words):
        return "summary"

    return "general"


def _estimate_risk(text_lower: str) -> str:
    """Estimate the risk level of a task."""
    if any(_word_match(text_lower, w) for w in _HIGH_RISK_WORDS):
        return "high"
    if any(_word_match(text_lower, w) for w in _MEDIUM_RISK_WORDS):
        return "medium"
    return "low"


def _estimate_priority(text_lower: str, risk: str) -> str:
    """Estimate task priority."""
    urgent_words = ["urgent", "asap", "immediately", "now", "緊急", "馬上", "立刻"]
    if any(w in text_lower for w in urgent_words):
        return "high"
    if risk == "high":
        return "high"
    return "medium"
