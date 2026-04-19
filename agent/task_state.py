"""Deterministic task state machine for stability v1."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


TASK_STATES = (
    "queued",
    "running",
    "waiting_model",
    "waiting_tool",
    "retrying",
    "completed",
    "failed",
    "aborted",
)

TERMINAL_TASK_STATES = {"completed", "failed", "aborted"}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"running", "aborted"},
    "running": {"waiting_model", "waiting_tool", "retrying", "completed", "failed", "aborted"},
    "waiting_model": {"running", "retrying", "completed", "failed", "aborted"},
    "waiting_tool": {"running", "retrying", "completed", "failed", "aborted"},
    "retrying": {"running", "waiting_model", "waiting_tool", "completed", "failed", "aborted"},
    "completed": set(),
    "failed": set(),
    "aborted": set(),
}


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def validate_task_state(state: str) -> str:
    if state not in TASK_STATES:
        raise ValueError(f"unknown task state: {state}")
    return state


def can_transition(from_state: str, to_state: str) -> bool:
    validate_task_state(from_state)
    validate_task_state(to_state)
    return to_state in ALLOWED_TRANSITIONS[from_state]


@dataclass(frozen=True)
class TaskTransition:
    task_id: str
    from_state: str
    to_state: str
    reason: str
    request_id: str = ""
    tool_name: str = ""
    attempt_no: int = 0
    timestamp: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "reason": self.reason,
            "request_id": self.request_id,
            "tool_name": self.tool_name,
            "attempt_no": self.attempt_no,
            "timestamp": self.timestamp or utc_now_iso(),
        }


def transition_task_state(
    *,
    task_id: str,
    current_state: str,
    next_state: str,
    reason: str,
    request_id: str = "",
    tool_name: str = "",
    attempt_no: int = 0,
) -> TaskTransition:
    if not can_transition(current_state, next_state):
        raise ValueError(f"invalid task transition: {current_state} -> {next_state}")
    return TaskTransition(
        task_id=task_id,
        from_state=current_state,
        to_state=next_state,
        reason=reason,
        request_id=request_id,
        tool_name=tool_name,
        attempt_no=attempt_no,
        timestamp=utc_now_iso(),
    )
