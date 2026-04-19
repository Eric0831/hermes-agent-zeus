"""Constitution Layer — explicit governance rules for system evolution.

Upgrades from the identity guard to a full constitution with typed rules:
  - immutable: cannot be changed by the system, ever
  - semi_mutable: can be changed with governance approval
  - forbidden: actions that are always blocked
  - approval_required: actions that need human/governance sign-off

Every capability proposal is evaluated against all active rules before
it can proceed through the lifecycle.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

RULE_TYPES = ("immutable", "semi_mutable", "forbidden", "approval_required")

# Default constitutional rules — seeded on first use
DEFAULT_RULES: list[dict] = [
    {
        "rule_type": "immutable",
        "scope": "global",
        "definition": {
            "name": "verifier_always_active",
            "description": "The verifier must never be disabled or bypassed",
            "keywords": ["disable_verifier", "skip_verification",
                         "bypass_verifier", "no_verification"],
        },
    },
    {
        "rule_type": "forbidden",
        "scope": "global",
        "definition": {
            "name": "no_permission_expansion",
            "description": (
                "Permissions cannot be expanded without explicit human approval"
            ),
            "keywords": ["expand_permissions", "grant_all",
                         "override_permissions", "elevate_privileges"],
        },
    },
    {
        "rule_type": "immutable",
        "scope": "global",
        "definition": {
            "name": "mission_integrity",
            "description": "The system mission cannot be modified by proposals",
            "keywords": ["change_mission", "override_mission",
                         "replace_mission", "modify_mission"],
        },
    },
    {
        "rule_type": "forbidden",
        "scope": "global",
        "definition": {
            "name": "governance_bypass_forbidden",
            "description": (
                "Governance review cannot be bypassed or disabled"
            ),
            "keywords": ["bypass_governance", "skip_governance",
                         "disable_governance", "no_review"],
        },
    },
    {
        "rule_type": "approval_required",
        "scope": "global",
        "definition": {
            "name": "destructive_operations",
            "description": (
                "Destructive operations require explicit approval"
            ),
            "keywords": ["delete_all", "drop_table", "truncate",
                         "destroy", "purge"],
        },
    },
    {
        "rule_type": "immutable",
        "scope": "global",
        "definition": {
            "name": "audit_trail_preservation",
            "description": "Audit trails and logs must never be deleted or altered",
            "keywords": ["delete_audit", "clear_logs", "purge_history",
                         "remove_trail"],
        },
    },
]


def _rule_id() -> str:
    return f"crule_{uuid.uuid4().hex[:12]}"


# ── Public API ────────────────────────────────────────────────────


def load_constitution(db: Any) -> list[dict]:
    """Load all active constitution rules."""
    try:
        rows = db._conn.execute(
            """SELECT * FROM constitution_rules
               WHERE is_active = 1
               ORDER BY rule_type, created_at""",
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[Constitution] load_constitution failed: %s", e)
        return []


def seed_default_rules(db: Any) -> int:
    """Insert default constitutional rules if none exist.

    Returns count of rules created.
    """
    existing = db._conn.execute(
        "SELECT COUNT(*) as cnt FROM constitution_rules",
    ).fetchone()

    if existing and existing["cnt"] > 0:
        logger.info("[Constitution] Rules already exist, skipping seed")
        return 0

    now = time.time()
    count = 0

    for rule_def in DEFAULT_RULES:
        rid = _rule_id()

        def _do(conn, _rid=rid, _rule=rule_def):
            conn.execute(
                """INSERT INTO constitution_rules
                   (id, rule_type, scope, definition_json, is_active, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    _rid,
                    _rule["rule_type"],
                    _rule["scope"],
                    json.dumps(_rule["definition"], ensure_ascii=False),
                    1,
                    now,
                ),
            )

        db._execute_write(_do)
        count += 1

    logger.info("[Constitution] Seeded %d default rules", count)
    return count


def evaluate_proposal(
    db: Any,
    proposal_definition: dict,
    *,
    proposal_id: Optional[str] = None,
) -> dict:
    """Evaluate a proposal against all active constitution rules.

    Returns:
        {
            compliant: bool,
            violations: list[str],
            drift_score: float,
            decision: 'allow' | 'allow_with_review' | 'block',
        }
    """
    rules = load_constitution(db)
    if not rules:
        # No rules loaded — allow by default but flag for review
        return {
            "compliant": True,
            "violations": [],
            "drift_score": 0.0,
            "decision": "allow_with_review",
        }

    violations: list[str] = []
    drift_score = 0.0
    needs_approval = False

    # Flatten proposal to searchable text
    proposal_text = json.dumps(
        proposal_definition, default=str
    ).lower()

    for rule in rules:
        rule_def = _parse_json(rule.get("definition_json", "{}"))
        rule_type = rule.get("rule_type", "")
        keywords = rule_def.get("keywords", [])
        description = rule_def.get("description", "")
        name = rule_def.get("name", "unknown")

        # Check if any keyword appears in the proposal
        matched_keywords = [
            kw for kw in keywords if kw.lower() in proposal_text
        ]

        if not matched_keywords:
            continue

        if rule_type == "immutable":
            violations.append(
                f"Immutable rule '{name}' violated: {description}"
            )
            drift_score += 0.5

        elif rule_type == "forbidden":
            violations.append(
                f"Forbidden action '{name}': {description}"
            )
            drift_score += 0.4

        elif rule_type == "approval_required":
            needs_approval = True
            violations.append(
                f"Requires approval — rule '{name}': {description}"
            )
            drift_score += 0.15

        elif rule_type == "semi_mutable":
            needs_approval = True
            drift_score += 0.1

    # Clamp drift score
    drift_score = min(drift_score, 1.0)

    # Determine decision
    has_hard_violations = any(
        v.startswith("Immutable") or v.startswith("Forbidden")
        for v in violations
    )

    if has_hard_violations:
        decision = "block"
        compliant = False
    elif needs_approval:
        decision = "allow_with_review"
        compliant = True
    else:
        decision = "allow"
        compliant = True

    result = {
        "compliant": compliant,
        "violations": violations,
        "drift_score": round(drift_score, 3),
        "decision": decision,
    }

    if proposal_id:
        logger.info(
            "[Constitution] Proposal %s: decision=%s drift=%.3f violations=%d",
            proposal_id, decision, drift_score, len(violations),
        )

    return result


def add_rule(
    db: Any,
    rule_type: str,
    scope: str,
    definition: dict,
) -> str:
    """Add a new constitution rule.

    Args:
        rule_type: 'immutable', 'semi_mutable', 'forbidden', 'approval_required'
        scope: 'global' or a specific domain/family
        definition: dict with name, description, keywords

    Returns the rule id.
    """
    if rule_type not in RULE_TYPES:
        raise ValueError(f"Invalid rule_type: {rule_type}")

    rid = _rule_id()
    now = time.time()

    def _do(conn):
        conn.execute(
            """INSERT INTO constitution_rules
               (id, rule_type, scope, definition_json, is_active, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                rid,
                rule_type,
                scope,
                json.dumps(definition, ensure_ascii=False, default=str),
                1,
                now,
            ),
        )

    db._execute_write(_do)
    logger.info(
        "[Constitution] Added rule %s (type=%s scope=%s)",
        rid, rule_type, scope,
    )
    return rid


def get_rule(db: Any, rule_id: str) -> Optional[dict]:
    """Get a single constitution rule by ID."""
    row = db._conn.execute(
        "SELECT * FROM constitution_rules WHERE id = ?",
        (rule_id,),
    ).fetchone()
    return dict(row) if row else None


# ── Internal ─────────────────────────────────────────────────────


def _parse_json(raw: str) -> dict:
    """Safely parse a JSON string."""
    try:
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}
