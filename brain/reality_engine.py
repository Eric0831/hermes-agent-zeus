"""Reality Engine — detects world-model invalidation and manages reconstruction.

When the system's assumptions about reality break down (tools stop working,
evidence patterns shift dramatically, doctrine becomes inapplicable), this
module detects the invalidation and coordinates reconstruction of a viable
world-model.

Severity levels:
  none     — no invalidation detected
  partial  — some signals indicate model degradation
  complete — world-model is fundamentally broken
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _psid() -> str:
    return f"pshf_{uuid.uuid4().hex[:12]}"


# -- Invalidation Detection --------------------------------------------------


def detect_invalidation(
    db: Any,
    *,
    check_evidence: bool = True,
    check_tools: bool = True,
    check_doctrine: bool = True,
) -> dict:
    """Analyze current state for reality model problems.

    Checks:
      - Task failure rate spike (recent tasks with status='failed')
      - Evidence type distribution shift
      - Tool success rate drop

    Returns:
        {is_invalid, signals, severity, recommended_action}
    """
    signals: list[str] = []

    try:
        if check_evidence:
            _check_evidence_signals(db, signals)
        if check_tools:
            _check_tool_signals(db, signals)
        if check_doctrine:
            _check_doctrine_signals(db, signals)
    except Exception as e:
        logger.warning("[RealityEngine] Detection error (non-fatal): %s", e)
        signals.append(f"detection_error: {e}")

    # Determine severity from signal count
    if len(signals) == 0:
        severity = "none"
        recommended_action = None
    elif len(signals) <= 2:
        severity = "partial"
        recommended_action = "monitor_and_adapt"
    else:
        severity = "complete"
        recommended_action = "full_reconstruction"

    result = {
        "is_invalid": len(signals) > 0,
        "signals": signals,
        "severity": severity,
        "recommended_action": recommended_action,
    }
    logger.info(
        "[RealityEngine] Invalidation check: severity=%s signals=%d",
        severity, len(signals),
    )
    return result


def _check_evidence_signals(db: Any, signals: list[str]) -> None:
    """Check for evidence distribution anomalies."""
    try:
        row = db._conn.execute(
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN source_type = 'tool_output' THEN 1 ELSE 0 END) as tool_ct
               FROM evidence_records
               WHERE created_at > ?""",
            (time.time() - 3600,),
        ).fetchone()
        if row:
            r = dict(row)
            total = r.get("total", 0)
            tool_ct = r.get("tool_ct", 0)
            if total > 10 and tool_ct / total < 0.2:
                signals.append("evidence_distribution_shift: low tool_output ratio")
    except Exception:
        pass  # table may not have data yet


def _check_tool_signals(db: Any, signals: list[str]) -> None:
    """Check for task failure rate spikes."""
    try:
        row = db._conn.execute(
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
               FROM tasks
               WHERE created_at > ?""",
            (time.time() - 3600,),
        ).fetchone()
        if row:
            r = dict(row)
            total = r.get("total", 0)
            failed = r.get("failed", 0)
            if total > 5 and failed / total > 0.5:
                signals.append(
                    f"task_failure_spike: {failed}/{total} tasks failed in last hour"
                )
    except Exception:
        pass


def _check_doctrine_signals(db: Any, signals: list[str]) -> None:
    """Check for doctrine applicability issues."""
    try:
        row = db._conn.execute(
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN status = 'deprecated' THEN 1 ELSE 0 END) as deprecated
               FROM doctrine_registry""",
        ).fetchone()
        if row:
            r = dict(row)
            total = r.get("total", 0)
            deprecated = r.get("deprecated", 0)
            if total > 0 and deprecated / total > 0.5:
                signals.append(
                    f"doctrine_degradation: {deprecated}/{total} doctrines deprecated"
                )
    except Exception:
        pass


# -- Reconstruction ----------------------------------------------------------


def start_reconstruction(
    db: Any,
    trigger_type: str,
    input_data: dict | str,
    *,
    epoch_id: Optional[str] = None,
) -> str:
    """Start a reality reconstruction process.

    Reuses the paradigm_shifts table with shift_type='reconstruction'.

    Returns the shift id.
    """
    shift_id = _psid()
    now = time.time()
    desc = {
        "trigger_type": trigger_type,
        "input_data": input_data if isinstance(input_data, dict) else {"raw": input_data},
        "phase": "started",
    }
    desc_json = json.dumps(desc, ensure_ascii=False)

    def _do(conn):
        conn.execute(
            """INSERT INTO paradigm_shifts
               (id, shift_type, description_json, severity, status, epoch_id,
                created_at, resolved_at)
               VALUES (?, 'reconstruction', ?, 'high', 'detected', ?, ?, NULL)""",
            (shift_id, desc_json, epoch_id, now),
        )

    db._execute_write(_do)
    logger.info(
        "[RealityEngine] Started reconstruction %s: trigger=%s",
        shift_id, trigger_type,
    )
    return shift_id


def complete_reconstruction(
    db: Any,
    shift_id: str,
    output_data: dict | str,
    validity_score: float,
) -> None:
    """Complete a reconstruction, recording results."""
    now = time.time()

    # Read current description to merge output into it
    try:
        row = db._conn.execute(
            "SELECT description_json FROM paradigm_shifts WHERE id = ?",
            (shift_id,),
        ).fetchone()
        if row:
            desc = json.loads(dict(row)["description_json"])
        else:
            desc = {}
    except Exception:
        desc = {}

    desc["output_data"] = (
        output_data if isinstance(output_data, dict) else {"raw": output_data}
    )
    desc["validity_score"] = validity_score
    desc["phase"] = "completed"
    desc_json = json.dumps(desc, ensure_ascii=False)

    def _do(conn):
        conn.execute(
            """UPDATE paradigm_shifts
               SET description_json = ?, status = 'resolved', resolved_at = ?
               WHERE id = ?""",
            (desc_json, now, shift_id),
        )

    db._execute_write(_do)
    logger.info(
        "[RealityEngine] Completed reconstruction %s: validity=%.3f",
        shift_id, validity_score,
    )


def get_reconstruction(db: Any, shift_id: str) -> Optional[dict]:
    """Get a reconstruction record by shift ID."""
    try:
        row = db._conn.execute(
            "SELECT * FROM paradigm_shifts WHERE id = ? AND shift_type = 'reconstruction'",
            (shift_id,),
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("[RealityEngine] get_reconstruction failed: %s", e)
        return None
