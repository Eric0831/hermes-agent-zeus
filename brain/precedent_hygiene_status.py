"""Read-only precedent hygiene status for gateway commands."""
from __future__ import annotations

import json
from collections import Counter
from typing import Any

from brain.precedent_hygiene import DEFAULT_MIN_EVIDENCE, is_clean_precedent_row


def collect_status(
    db: Any,
    *,
    limit: int = 8,
    min_evidence: int = DEFAULT_MIN_EVIDENCE,
) -> dict[str, Any]:
    """Collect precedent hygiene stats for one gateway SessionDB."""
    if db is None:
        return {"present": False, "reason": "no_db"}

    try:
        rows = db._conn.execute(
            """SELECT id, precedent_type, subject_type, subject_id,
                      decision_json, binding_strength, created_at
               FROM precedent_records
               WHERE subject_type = 'task_family'
               ORDER BY created_at DESC"""
        ).fetchall()
    except Exception as exc:
        return {"present": False, "reason": str(exc)}

    total = len(rows)
    clean = 0
    rejected = 0
    reason_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []

    for row in rows:
        item = dict(row)
        ok, reason = is_clean_precedent_row(item, min_evidence=min_evidence)
        family = str(item.get("subject_id") or "unknown")
        if ok:
            clean += 1
            continue

        rejected += 1
        reason_counts[reason] += 1
        family_counts[family] += 1
        if len(samples) < limit:
            decision = _load_json(item.get("decision_json"))
            samples.append({
                "id": item.get("id"),
                "family": family,
                "reason": reason,
                "binding_strength": float(item.get("binding_strength") or 0.0),
                "evidence_count": _as_int(decision.get("evidence_count")),
                "goal": _compact(str(decision.get("goal") or ""), 120),
            })

    return {
        "present": True,
        "min_evidence": min_evidence,
        "total_task_family_precedents": total,
        "clean": clean,
        "rejected": rejected,
        "rejected_pct": round(100.0 * rejected / total, 1) if total else 0.0,
        "reason_counts": dict(reason_counts.most_common()),
        "family_counts": dict(family_counts.most_common()),
        "samples": samples,
    }


def format_status(snapshot: dict[str, Any]) -> str:
    """Format precedent hygiene status as compact Markdown."""
    if not snapshot.get("present"):
        return f"Precedent hygiene status unavailable: {snapshot.get('reason') or 'unknown'}"

    lines = [
        "**Precedent Hygiene**",
        "",
        (
            f"Task-family precedents: total={snapshot['total_task_family_precedents']} "
            f"clean={snapshot['clean']} rejected={snapshot['rejected']} "
            f"({snapshot['rejected_pct']:.1f}%)"
        ),
        f"Minimum evidence: {snapshot['min_evidence']}",
    ]

    if snapshot["reason_counts"]:
        reasons = ", ".join(
            f"{reason}={count}"
            for reason, count in snapshot["reason_counts"].items()
        )
        lines.append(f"Reject reasons: {reasons}")
    if snapshot["family_counts"]:
        families = ", ".join(
            f"{family}={count}"
            for family, count in list(snapshot["family_counts"].items())[:5]
        )
        lines.append(f"Families: {families}")

    lines.append("")
    lines.append(f"Rejected samples ({len(snapshot['samples'])} shown):")
    if not snapshot["samples"]:
        lines.append("  (none)")
    for sample in snapshot["samples"]:
        lines.append(
            f"  `{sample['id']}` [{sample['family']}] {sample['reason']} "
            f"evidence={sample['evidence_count']} bind={sample['binding_strength']:.2f}"
        )
        if sample["goal"]:
            lines.append(f"    {sample['goal']}")

    lines.append("")
    lines.append("Read-only report. Rejected precedents are hidden from Planner recall, not deleted.")
    return "\n".join(lines)


def _load_json(raw: Any) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _compact(text: str, limit: int) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)] + "..."
