"""Replicator Dynamics — weight updates for competing evolution units.

Implements governed replicator dynamics within a family of units:
  p_i(t+1) = p_i(t) * F_i / F_bar

Where F_i is the effective fitness (with governance penalty applied)
and F_bar is the mean fitness across the family. Weights are normalized
to sum to 1.0 after each update.

Governance penalty eta=0.35 is applied to units with conditional governance
status, and fitness is zeroed for governance-failed units.
"""

from __future__ import annotations

import json
import logging
import random
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────

GOVERNANCE_PENALTY_ETA = 0.35


def _weight_id() -> str:
    return f"rw_{uuid.uuid4().hex[:12]}"


# ── Weight Updates ───────────────────────────────────────────────


def update_weights(
    db: Any,
    family: str,
    window_label: str,
) -> list[dict]:
    """Compute governed replicator update for all adopted/trial units in a family.

    p_i(t+1) = p_i(t) * F_i / F_bar
    with governance penalty eta for conditional units, 0 for failed.
    Normalizes weights to sum to 1.0.

    Returns [{unit_id, weight}].
    """
    # Get all active units in the family
    units = db._conn.execute(
        """SELECT id, status FROM evolution_units
           WHERE family = ? AND status IN ('adopted', 'trial')""",
        (family,),
    ).fetchall()

    if not units:
        logger.info("No active units in family %s for replicator update", family)
        return []

    unit_list = [dict(u) for u in units]

    # Get current weights (or default to uniform)
    current_weights = _get_current_weights_map(db, family)
    uniform = 1.0 / len(unit_list)

    # Get latest fitness for each unit and apply governance penalty
    effective_fitness: list[tuple[str, float, float]] = []  # (unit_id, weight, fitness)

    for u in unit_list:
        uid = u["id"]
        w_i = current_weights.get(uid, uniform)

        # Fetch latest fitness
        fit_row = db._conn.execute(
            """SELECT score FROM fitness_runs
               WHERE unit_id = ? ORDER BY created_at DESC LIMIT 1""",
            (uid,),
        ).fetchone()
        raw_fitness = fit_row["score"] if fit_row else 0.5

        # Apply governance penalty
        if u["status"] == "trial":
            f_i = raw_fitness * (1.0 - GOVERNANCE_PENALTY_ETA)
        else:
            f_i = raw_fitness

        effective_fitness.append((uid, w_i, f_i))

    # Compute mean fitness (F_bar)
    if not effective_fitness:
        return []

    f_bar = sum(f for _, _, f in effective_fitness) / len(effective_fitness)

    # Replicator update: p_i(t+1) = p_i(t) * F_i / F_bar
    new_weights: list[tuple[str, float]] = []
    if f_bar > 0:
        for uid, w_i, f_i in effective_fitness:
            new_w = w_i * f_i / f_bar
            new_weights.append((uid, new_w))
    else:
        # If mean fitness is zero, fall back to uniform
        for uid, _, _ in effective_fitness:
            new_weights.append((uid, uniform))

    # Normalize to sum to 1.0
    total_w = sum(w for _, w in new_weights)
    if total_w > 0:
        new_weights = [(uid, w / total_w) for uid, w in new_weights]
    else:
        new_weights = [(uid, uniform) for uid, _ in new_weights]

    # Store weights
    now = time.time()

    def _do(conn):
        for uid, w in new_weights:
            wid = _weight_id()
            conn.execute(
                """INSERT INTO replicator_weights
                   (id, family, unit_id, weight, window_label, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (wid, family, uid, w, window_label, now),
            )

    db._execute_write(_do)

    result = [{"unit_id": uid, "weight": w} for uid, w in new_weights]
    logger.info(
        "Replicator update for family %s (window=%s): %d units",
        family, window_label, len(result),
    )
    return result


# ── Weight Queries ───────────────────────────────────────────────


def get_weights(db: Any, family: str) -> list[dict]:
    """Get the latest weights for a family (most recent window_label)."""
    # Find latest window_label
    latest = db._conn.execute(
        """SELECT window_label FROM replicator_weights
           WHERE family = ? ORDER BY created_at DESC LIMIT 1""",
        (family,),
    ).fetchone()

    if not latest:
        return []

    rows = db._conn.execute(
        """SELECT unit_id, weight, window_label, created_at
           FROM replicator_weights
           WHERE family = ? AND window_label = ?
           ORDER BY weight DESC""",
        (family, latest["window_label"]),
    ).fetchall()

    return [dict(r) for r in rows]


def get_weight_history(db: Any, family: str, limit: int = 10) -> list[dict]:
    """Get weight snapshots over time for a family.

    Returns distinct window labels with their weight distributions.
    """
    # Get distinct window labels
    labels = db._conn.execute(
        """SELECT DISTINCT window_label, MAX(created_at) as ts
           FROM replicator_weights
           WHERE family = ?
           GROUP BY window_label
           ORDER BY ts DESC LIMIT ?""",
        (family, limit),
    ).fetchall()

    history = []
    for label_row in labels:
        wl = label_row["window_label"]
        rows = db._conn.execute(
            """SELECT unit_id, weight FROM replicator_weights
               WHERE family = ? AND window_label = ?""",
            (family, wl),
        ).fetchall()
        history.append({
            "window_label": wl,
            "created_at": label_row["ts"],
            "weights": [dict(r) for r in rows],
        })
    return history


def select_by_weight(db: Any, family: str) -> Optional[str]:
    """Probabilistically select a unit_id based on current weights.

    Uses weighted random selection. Returns None if no weights exist.
    """
    weights = get_weights(db, family)
    if not weights:
        return None

    unit_ids = [w["unit_id"] for w in weights]
    w_values = [w["weight"] for w in weights]

    # Weighted random selection
    total = sum(w_values)
    if total <= 0:
        return random.choice(unit_ids) if unit_ids else None

    r = random.random() * total
    cumulative = 0.0
    for uid, w in zip(unit_ids, w_values):
        cumulative += w
        if r <= cumulative:
            return uid

    # Fallback (should not reach here)
    return unit_ids[-1]


# ── Internal Helpers ─────────────────────────────────────────────


def _get_current_weights_map(db: Any, family: str) -> dict[str, float]:
    """Get current weights as {unit_id: weight} map."""
    weights = get_weights(db, family)
    return {w["unit_id"]: w["weight"] for w in weights}
