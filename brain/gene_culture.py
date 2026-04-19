"""Gene-Culture Coevolution — dual inheritance dynamics.

Manages the interplay between slow gene-like core traits (macro/civilizational
layers) and fast culture-like adaptations (micro layer).  Units at the meso
layer exhibit hybrid inheritance.

Transmission tracking records how traits propagate across scopes, enforcing
governance and risk constraints for horizontal transfers.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Classification Rules ────────────────────────────────────────

_GENE_LIKE_LAYERS = ("macro", "civilizational", "trans_civilizational")
_CULTURE_LIKE_LAYERS = ("micro",)
_HYBRID_LAYERS = ("meso",)

TRANSMISSION_KINDS = (
    "vertical_gene",
    "horizontal_culture",
    "hybrid_transfer",
)


# ── ID Generation ───────────────────────────────────────────────


def _transmission_id() -> str:
    return f"gct_{uuid.uuid4().hex[:12]}"


# ── Classification ──────────────────────────────────────────────


def classify_unit(unit: dict) -> dict:
    """Determine inheritance, transmission, and stability class for a unit.

    Rules:
      - macro / civilizational / trans_civilizational →
            gene_like / vertical_only / stable_core
      - micro → culture_like / horizontal_allowed / high_variation
      - meso  → hybrid / mixed / adaptive_mid
      - fallback (unknown layer) → culture_like defaults
    """
    layer = unit.get("layer", "")

    if layer in _GENE_LIKE_LAYERS:
        return {
            "inheritance_mode": "gene_like",
            "transmission_mode": "vertical_only",
            "stability_class": "stable_core",
        }
    if layer in _HYBRID_LAYERS:
        return {
            "inheritance_mode": "hybrid",
            "transmission_mode": "mixed",
            "stability_class": "adaptive_mid",
        }
    # micro or unknown
    return {
        "inheritance_mode": "culture_like",
        "transmission_mode": "horizontal_allowed",
        "stability_class": "high_variation",
    }


# ── Transmission Gate ───────────────────────────────────────────


def can_transmit_horizontally(db: Any, unit_id: str) -> bool:
    """Check whether *unit_id* is eligible for horizontal transmission.

    Requires:
      1. Unit's transmission_mode allows horizontal (not vertical_only).
      2. No failed governance decision for this unit.
      3. Risk level is not 'critical'.
    """
    row = db._conn.execute(
        "SELECT transmission_mode, risk_level FROM evolution_units WHERE id = ?",
        (unit_id,),
    ).fetchone()

    if not row:
        return False

    d = dict(row)
    if d.get("transmission_mode") == "vertical_only":
        return False
    if d.get("risk_level") == "critical":
        return False

    # Check governance: any rejected selection decision blocks transmission
    try:
        gov_row = db._conn.execute(
            """SELECT COUNT(*) AS cnt FROM selection_decisions
               WHERE unit_id = ? AND decision = 'reject'""",
            (unit_id,),
        ).fetchone()
        if gov_row and dict(gov_row).get("cnt", 0) > 0:
            return False
    except Exception:
        pass  # table may not exist yet

    return True


# ── Record Transmission ────────────────────────────────────────


def record_transmission(
    db: Any,
    unit_id: str,
    kind: str,
    source_scope: str,
    target_scope: str,
    decision: str,
) -> str:
    """Write a transmission event. Returns transmission_id."""
    tid = _transmission_id()
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO gene_culture_transmissions
               (id, unit_id, transmission_kind, source_scope, target_scope,
                decision, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (tid, unit_id, kind, source_scope, target_scope, decision, now),
        )

    db._execute_write(_do)
    logger.info(
        "Transmission %s: unit=%s kind=%s %s->%s decision=%s",
        tid, unit_id, kind, source_scope, target_scope, decision,
    )
    return tid


# ── Query Transmissions ────────────────────────────────────────


def get_transmissions(
    db: Any,
    unit_id: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """Fetch recent transmissions, optionally filtered by unit_id."""
    if unit_id:
        rows = db._conn.execute(
            """SELECT * FROM gene_culture_transmissions
               WHERE unit_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (unit_id, limit),
        ).fetchall()
    else:
        rows = db._conn.execute(
            """SELECT * FROM gene_culture_transmissions
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    return [dict(r) for r in rows]


# ── Fitness Decomposition ──────────────────────────────────────

_RHO_MAP = {
    "stable_core": 0.8,
    "adaptive_mid": 0.5,
    "high_variation": 0.2,
}


def calculate_gene_culture_fitness(
    base_fitness: float,
    stability_class: str,
) -> dict:
    """Split base fitness into gene and culture components.

    F_gene    = rho * F_base
    F_culture = (1 - rho) * F_base
    F_total   = F_gene + F_culture  (== F_base)

    rho depends on stability_class:
      stable_core   → 0.8
      adaptive_mid  → 0.5
      high_variation→ 0.2
    """
    rho = _RHO_MAP.get(stability_class, 0.5)
    f_gene = rho * base_fitness
    f_culture = (1 - rho) * base_fitness

    return {
        "f_gene": round(f_gene, 6),
        "f_culture": round(f_culture, 6),
        "f_total": round(f_gene + f_culture, 6),
        "rho": rho,
    }
