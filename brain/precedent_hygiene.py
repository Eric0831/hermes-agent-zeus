"""Precedent hygiene filters.

Precedents are useful only when they represent clean, reusable decision
patterns. This module keeps media/image tasks, very low-evidence cases,
and obvious task-family mismatches out of planner recall and future
precedent extraction.
"""
from __future__ import annotations

import json
import re
from typing import Any


DEFAULT_MIN_EVIDENCE = 5

_MEDIA_PATTERNS = (
    "image_url",
    "image_cache",
    "vision_analyze",
    "sent an image",
    "couldn't quite see",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    "圖片",
    "影像",
    "照片",
    "改圖",
    "和服風",
    "性感日式和服",
)

_CODING_HINTS = (
    "fix", "debug", "test", "implement", "code", "patch", "compile",
    "model", "service", "runtime", "api", "data lake", "embedding",
    "修復", "測試", "檢查", "修改", "程式", "服務", "模型", "系統",
)


def is_clean_task_precedent(
    *,
    family: str,
    goal: str,
    evidence_count: int,
    min_evidence: int = DEFAULT_MIN_EVIDENCE,
) -> tuple[bool, str]:
    """Return whether a completed task should become a reusable precedent."""
    fam = (family or "").strip().lower()
    text = _normalize(goal)

    if int(evidence_count or 0) < min_evidence:
        return False, f"low_evidence:{evidence_count}<{min_evidence}"

    if _looks_like_media_task(text):
        return False, "media_or_image_task"

    if fam == "coding" and not _looks_like_coding_task(text):
        return False, "coding_family_mismatch"

    return True, "ok"


def is_clean_precedent_row(
    row: dict[str, Any],
    *,
    min_evidence: int = DEFAULT_MIN_EVIDENCE,
) -> tuple[bool, str]:
    """Return whether an existing precedent row is suitable for recall."""
    decision = _loads(row.get("decision_json"))
    if not isinstance(decision, dict):
        return False, "invalid_decision_json"

    family = str(
        decision.get("family")
        or row.get("subject_id")
        or row.get("subject_type")
        or ""
    )
    goal = str(decision.get("goal") or decision.get("decision") or "")
    evidence_count = _as_int(decision.get("evidence_count"))
    return is_clean_task_precedent(
        family=family,
        goal=goal,
        evidence_count=evidence_count,
        min_evidence=min_evidence,
    )


def filter_clean_precedents(
    rows: list[dict[str, Any]],
    *,
    min_evidence: int = DEFAULT_MIN_EVIDENCE,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Filter precedent rows for Planner context."""
    clean: list[dict[str, Any]] = []
    for row in rows:
        ok, reason = is_clean_precedent_row(row, min_evidence=min_evidence)
        if ok:
            clean.append(row)
            if limit is not None and len(clean) >= limit:
                break
        else:
            row["_hygiene_reject_reason"] = reason
    return clean


def _looks_like_media_task(text: str) -> bool:
    return any(pattern in text for pattern in _MEDIA_PATTERNS)


def _looks_like_coding_task(text: str) -> bool:
    return any(hint in text for hint in _CODING_HINTS)


def _normalize(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _loads(raw: Any) -> Any:
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        match = re.search(r"\d+", str(value or ""))
        return int(match.group(0)) if match else 0
