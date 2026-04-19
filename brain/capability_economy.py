"""Capability Economy — resource allocation, valuation, and retirement.

Evaluates the value of capabilities (skill families) based on usage,
success rate, and recency. Provides recommendations for investment,
retention, review, or retirement of capability families.

No new tables — reads from skill_registry, skill_applications,
and evidence_records.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Valuation weights
USAGE_WEIGHT = 0.3
SUCCESS_WEIGHT = 0.4
RECENCY_WEIGHT = 0.3

# Recency decay: score = 1.0 if used in last day, decays toward 0
RECENCY_HALF_LIFE_DAYS = 14.0


def _recency_score(last_used_at: float | None) -> float:
    """Compute recency score (0-1) based on time since last use."""
    if not last_used_at:
        return 0.0
    age_days = (time.time() - last_used_at) / 86400.0
    if age_days <= 0:
        return 1.0
    # Exponential decay with half-life
    import math
    return math.exp(-0.693 * age_days / RECENCY_HALF_LIFE_DAYS)


def _compute_recommendation(value_score: float) -> str:
    """Determine recommendation based on value score."""
    if value_score >= 0.7:
        return "retain"
    elif value_score >= 0.5:
        return "invest"
    elif value_score >= 0.2:
        return "review"
    else:
        return "retire"


# -- Valuation ---------------------------------------------------------------


def valuate_capability(db: Any, capability_family: str) -> dict:
    """Compute value for a single capability family.

    Aggregates usage_count, success_rate, and last_used recency from
    all skills in the family.

    Returns:
        {
            "family": str,
            "value_score": float (0-1),
            "usage": int,
            "success_rate": float,
            "recommendation": "retain" | "invest" | "review" | "retire"
        }
    """
    try:
        rows = db._conn.execute(
            """SELECT usage_count, success_rate, last_used_at
               FROM skill_registry
               WHERE intent_family = ? AND status = 'active'""",
            (capability_family,),
        ).fetchall()

        if not rows:
            return {
                "family": capability_family,
                "value_score": 0.0,
                "usage": 0,
                "success_rate": 0.0,
                "recommendation": "retire",
            }

        total_usage = sum(r["usage_count"] for r in rows)
        avg_success = (
            sum(r["success_rate"] for r in rows) / len(rows)
        )

        # Use the most recent last_used_at across all skills in the family
        last_used_times = [r["last_used_at"] for r in rows if r["last_used_at"]]
        most_recent = max(last_used_times) if last_used_times else None
        recency = _recency_score(most_recent)

        # Normalize usage (log scale, cap at 100 uses for max score)
        import math
        usage_norm = min(1.0, math.log1p(total_usage) / math.log1p(100))

        value_score = (
            USAGE_WEIGHT * usage_norm
            + SUCCESS_WEIGHT * avg_success
            + RECENCY_WEIGHT * recency
        )
        value_score = round(min(1.0, max(0.0, value_score)), 3)

        return {
            "family": capability_family,
            "value_score": value_score,
            "usage": total_usage,
            "success_rate": round(avg_success, 3),
            "recommendation": _compute_recommendation(value_score),
        }
    except Exception as e:
        logger.error("[CapEconomy] valuate_capability failed: %s", e)
        return {
            "family": capability_family,
            "value_score": 0.0,
            "usage": 0,
            "success_rate": 0.0,
            "recommendation": "review",
        }


def valuate_all(db: Any) -> list[dict]:
    """Valuate all active skill families.

    Returns a list of valuation dicts sorted by value_score descending.
    """
    try:
        rows = db._conn.execute(
            """SELECT DISTINCT intent_family
               FROM skill_registry WHERE status = 'active'""",
        ).fetchall()

        families = [r["intent_family"] for r in rows]
        results = [valuate_capability(db, f) for f in families]
        results.sort(key=lambda r: r["value_score"], reverse=True)
        return results
    except Exception as e:
        logger.error("[CapEconomy] valuate_all failed: %s", e)
        return []


def recommend_retirements(db: Any, threshold: float = 0.2) -> list[dict]:
    """Find capabilities below the threshold value score.

    Returns valuations for families that should be retired.
    """
    all_vals = valuate_all(db)
    return [v for v in all_vals if v["value_score"] < threshold]


def get_allocation_summary(db: Any) -> dict:
    """Get a summary of capability distribution.

    Returns:
        {
            "total_families": int,
            "total_skills": int,
            "by_recommendation": {"retain": N, "invest": N, ...},
            "avg_value_score": float,
            "top_families": [...],
            "retirement_candidates": int,
        }
    """
    try:
        all_vals = valuate_all(db)

        if not all_vals:
            return {
                "total_families": 0,
                "total_skills": 0,
                "by_recommendation": {},
                "avg_value_score": 0.0,
                "top_families": [],
                "retirement_candidates": 0,
            }

        by_rec: dict[str, int] = {}
        for v in all_vals:
            rec = v["recommendation"]
            by_rec[rec] = by_rec.get(rec, 0) + 1

        avg_val = sum(v["value_score"] for v in all_vals) / len(all_vals)

        # Total active skills
        skill_row = db._conn.execute(
            "SELECT COUNT(*) as cnt FROM skill_registry WHERE status = 'active'",
        ).fetchone()

        return {
            "total_families": len(all_vals),
            "total_skills": skill_row["cnt"] if skill_row else 0,
            "by_recommendation": by_rec,
            "avg_value_score": round(avg_val, 3),
            "top_families": all_vals[:5],
            "retirement_candidates": by_rec.get("retire", 0),
        }
    except Exception as e:
        logger.error("[CapEconomy] get_allocation_summary failed: %s", e)
        return {
            "total_families": 0,
            "total_skills": 0,
            "by_recommendation": {},
            "avg_value_score": 0.0,
            "top_families": [],
            "retirement_candidates": 0,
        }
