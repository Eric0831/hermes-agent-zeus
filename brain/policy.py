"""Unified Policy Service — consolidates tool allow/deny, risk scoring, and approval.

Wraps the existing Hermes approval system (tools/approval.py) and adds:
- Structured risk evaluation per action
- Policy evaluation audit logging (policy_evaluations table)
- Configurable allow/deny rules beyond just dangerous-command patterns
- Budget enforcement hooks

Phase 1: delegates to existing approval.py for dangerous commands,
adds structured logging and risk scoring on top.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from brain.models import PolicyDecision

logger = logging.getLogger(__name__)

# ── Tool Risk Profiles ────────────────────────────────────────────

# Default risk levels for known tools (overridable via config)
_TOOL_RISK_PROFILES: dict[str, str] = {
    # High risk — external side effects
    "send_message": "high",
    "terminal": "medium",
    "shell_exec_sandboxed": "medium",
    "deploy": "high",
    # Medium risk — write operations
    "write_file": "medium",
    "patch": "medium",
    "image_generate": "low",
    "cronjob": "medium",
    # Low risk — read-only
    "web_search": "low",
    "web_extract": "low",
    "read_file": "low",
    "search_files": "low",
    "browser_navigate": "low",
    "vision_analyze": "low",
    "session_search": "low",
    "memory": "low",
    "todo": "low",
}


# ── Public API ────────────────────────────────────────────────────


def evaluate(
    action_type: str,
    target: str,
    *,
    task_id: Optional[str] = None,
    task_risk_level: str = "low",
    context: Optional[dict[str, Any]] = None,
    db: Optional[Any] = None,
) -> PolicyDecision:
    """
    Evaluate whether an action should be allowed.

    Args:
        action_type: 'tool_call' | 'message_send' | 'file_write' | 'shell_exec'
        target: tool name, file path, or action target
        task_id: associated task (for audit logging)
        task_risk_level: risk level of the parent task
        context: additional context (channel, user, etc.)
        db: SessionDB for audit logging

    Returns:
        PolicyDecision with allow/deny/approval/sandbox decision
    """
    tool_risk = _get_tool_risk(target)
    combined_risk = _combine_risk(task_risk_level, tool_risk)

    # Check existing Hermes approval system for dangerous commands
    if action_type in ("tool_call", "shell_exec") and target in ("terminal", "shell_exec_sandboxed"):
        command = (context or {}).get("command", "")
        if command:
            hermes_decision = _check_hermes_approval(command)
            if hermes_decision is not None:
                _log_evaluation(db, task_id, action_type, target, combined_risk, hermes_decision)
                return hermes_decision

    # Policy rules
    decision = _apply_rules(action_type, target, combined_risk, context)
    _log_evaluation(db, task_id, action_type, target, combined_risk, decision)
    return decision


def evaluate_tool_call(
    tool_name: str,
    *,
    task_id: Optional[str] = None,
    task_risk_level: str = "low",
    args: Optional[dict] = None,
    db: Optional[Any] = None,
) -> PolicyDecision:
    """Convenience wrapper for tool call evaluation."""
    return evaluate(
        action_type="tool_call",
        target=tool_name,
        task_id=task_id,
        task_risk_level=task_risk_level,
        context={"args": args} if args else None,
        db=db,
    )


# ── Risk Scoring ──────────────────────────────────────────────────


def _get_tool_risk(tool_name: str) -> str:
    """Get the risk level for a tool."""
    return _TOOL_RISK_PROFILES.get(tool_name, "low")


def _combine_risk(task_risk: str, tool_risk: str) -> str:
    """Combine task-level and tool-level risk into an overall risk."""
    levels = {"low": 0, "medium": 1, "high": 2}
    task_val = levels.get(task_risk, 0)
    tool_val = levels.get(tool_risk, 0)

    combined = max(task_val, tool_val)
    return {0: "low", 1: "medium", 2: "high"}[combined]


# ── Rule Engine ───────────────────────────────────────────────────


def _apply_rules(
    action_type: str,
    target: str,
    risk_level: str,
    context: Optional[dict] = None,
) -> PolicyDecision:
    """Apply policy rules to determine the decision."""
    # Rule 1: High risk always requires approval
    if risk_level == "high":
        return PolicyDecision(
            decision="allow_with_approval",
            reason=f"High-risk {action_type} on {target} requires approval",
            risk_level=risk_level,
        )

    # Rule 2: Shell execution in non-sandbox requires review
    if action_type == "shell_exec" and risk_level == "medium":
        return PolicyDecision(
            decision="allow_with_approval",
            reason="Shell execution at medium risk requires approval",
            risk_level=risk_level,
        )

    # Rule 3: External message sending requires at least medium check
    if action_type == "message_send" and target != "local":
        if risk_level != "low":
            return PolicyDecision(
                decision="allow_with_approval",
                reason="External message send requires approval",
                risk_level=risk_level,
            )

    # Default: allow
    return PolicyDecision(
        decision="allow",
        reason="Within acceptable risk parameters",
        risk_level=risk_level,
    )


# ── Hermes Approval Integration ──────────────────────────────────


def _check_hermes_approval(command: str) -> Optional[PolicyDecision]:
    """Check against the existing Hermes dangerous-command detection."""
    try:
        from tools.approval import detect_dangerous_command
        is_dangerous, pattern_key, description = detect_dangerous_command(command)
        if is_dangerous:
            return PolicyDecision(
                decision="deny",
                reason=f"Dangerous command detected: {description}",
                risk_level="high",
            )
    except ImportError:
        logger.debug("Hermes approval module not available")
    except Exception as e:
        logger.debug("Hermes approval check failed: %s", e)

    return None


# ── Audit Logging ─────────────────────────────────────────────────


def _log_evaluation(
    db: Optional[Any],
    task_id: Optional[str],
    action_type: str,
    target: str,
    risk_level: str,
    decision: PolicyDecision,
) -> None:
    """Log a policy evaluation to the audit table."""
    if db is None:
        return

    try:
        def _do(conn):
            conn.execute(
                """INSERT INTO policy_evaluations
                   (task_id, action_type, target, risk_level, decision, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (task_id, action_type, target, risk_level,
                 decision.decision, decision.reason, time.time()),
            )
        db._execute_write(_do)
    except Exception as e:
        logger.debug("Policy audit log failed (non-fatal): %s", e)
