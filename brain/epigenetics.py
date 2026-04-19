"""Epigenetics — context-dependent expression modulation.

Same evolution unit can exhibit different activation levels depending on
context.  Epigenetic markers adjust expression weights that modify effective
fitness without changing the underlying unit definition.

Markers can be reversible, time-limited (expires_at), and subject to
gradual decay toward neutral (1.0).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────

ACTIVATION_STATES = ("suppressed", "neutral", "enhanced")

_EXPRESSION_COEFFICIENTS = {
    "suppressed": 0.4,
    "neutral": 1.0,
    "enhanced": 1.3,
}


# ── ID Generation ───────────────────────────────────────────────


def _marker_id() -> str:
    return f"epi_{uuid.uuid4().hex[:12]}"


# ── Marker CRUD ─────────────────────────────────────────────────


def create_marker(
    db: Any,
    unit_id: str,
    context_type: str,
    expression_weight: float,
    activation_state: str = "neutral",
    *,
    reversible: bool = True,
    expires_at: Optional[float] = None,
) -> str:
    """Create an epigenetic marker. Returns marker_id."""
    mid = _marker_id()
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO epigenetic_markers
               (id, unit_id, context_type, expression_weight, activation_state,
                reversible, expires_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                mid, unit_id, context_type, expression_weight,
                activation_state, int(reversible), expires_at, now, now,
            ),
        )

    db._execute_write(_do)
    logger.info(
        "Created marker %s: unit=%s ctx=%s state=%s weight=%.3f",
        mid, unit_id, context_type, activation_state, expression_weight,
    )
    return mid


def get_markers(db: Any, unit_id: str) -> list[dict]:
    """Fetch active, non-expired markers for a unit."""
    now = time.time()
    rows = db._conn.execute(
        """SELECT * FROM epigenetic_markers
           WHERE unit_id = ?
             AND (expires_at IS NULL OR expires_at > ?)
           ORDER BY created_at DESC""",
        (unit_id, now),
    ).fetchall()
    return [dict(r) for r in rows]


def get_marker(db: Any, marker_id: str) -> Optional[dict]:
    """Fetch a single marker by ID."""
    row = db._conn.execute(
        "SELECT * FROM epigenetic_markers WHERE id = ?",
        (marker_id,),
    ).fetchone()
    return dict(row) if row else None


def expire_marker(db: Any, marker_id: str) -> None:
    """Immediately expire a marker by setting expires_at to now."""
    now = time.time()

    def _do(conn):
        conn.execute(
            "UPDATE epigenetic_markers SET expires_at = ?, updated_at = ? WHERE id = ?",
            (now, now, marker_id),
        )

    db._execute_write(_do)
    logger.info("Expired marker %s", marker_id)


# ── Expression Logic ────────────────────────────────────────────


def apply_expression(
    base_fitness: float,
    markers: list[dict],
    context_type: str,
) -> float:
    """Compute effective fitness given epigenetic markers for a context.

    F_eff = F_base * E(context)

    E(context) is the expression coefficient of the matching marker's
    activation_state.  If multiple markers match, the last-created one wins.
    If no marker matches the context, F_base is returned unchanged.

    Coefficients:
      suppressed = 0.4
      neutral    = 1.0
      enhanced   = 1.3
    """
    matching = [m for m in markers if m.get("context_type") == context_type]
    if not matching:
        return base_fitness

    # Use the most recent matching marker (markers are ordered DESC by created_at)
    best = matching[0]
    state = best.get("activation_state", "neutral")
    coeff = _EXPRESSION_COEFFICIENTS.get(state, 1.0)
    return round(base_fitness * coeff, 6)


# ── Decay ───────────────────────────────────────────────────────


def decay_markers(db: Any, decay_rate: float = 0.05) -> int:
    """Gradually move expression weights toward neutral (1.0).

    - Weights > 1.0 are reduced by decay_rate.
    - Weights < 1.0 are increased by decay_rate.
    - Weights that cross or reach 1.0 are clamped to 1.0.

    Returns the count of markers adjusted.
    """
    now = time.time()
    rows = db._conn.execute(
        """SELECT id, expression_weight FROM epigenetic_markers
           WHERE (expires_at IS NULL OR expires_at > ?)
             AND expression_weight != 1.0""",
        (now,),
    ).fetchall()

    if not rows:
        return 0

    adjusted = 0
    for row in rows:
        d = dict(row)
        w = d["expression_weight"]
        mid = d["id"]

        if w > 1.0:
            new_w = max(1.0, w - decay_rate)
        else:
            new_w = min(1.0, w + decay_rate)

        if new_w != w:
            def _do(conn, _mid=mid, _new_w=new_w, _now=now):
                conn.execute(
                    """UPDATE epigenetic_markers
                       SET expression_weight = ?, updated_at = ?
                       WHERE id = ?""",
                    (_new_w, _now, _mid),
                )
            db._execute_write(_do)
            adjusted += 1

    logger.info("Decayed %d epigenetic markers (rate=%.3f)", adjusted, decay_rate)
    return adjusted


# ── Stats ───────────────────────────────────────────────────────


def get_marker_stats(db: Any) -> dict:
    """Return counts of active markers grouped by activation_state."""
    now = time.time()
    rows = db._conn.execute(
        """SELECT activation_state, COUNT(*) AS cnt
           FROM epigenetic_markers
           WHERE (expires_at IS NULL OR expires_at > ?)
           GROUP BY activation_state""",
        (now,),
    ).fetchall()

    stats: dict[str, int] = {s: 0 for s in ACTIVATION_STATES}
    for row in rows:
        d = dict(row)
        state = d.get("activation_state", "neutral")
        stats[state] = d.get("cnt", 0)
    return stats
