"""Caller↔callee signature compatibility tests.

Catches the class of bug that hit production 2026-04-26: ``run_agent._invoke_tool``
started passing ``session_db=...`` to ``model_tools.handle_function_call`` before
the callee's signature accepted that kwarg, causing every tool dispatch to
``TypeError`` until the working tree caught up.

This file enumerates every kwarg the caller passes (per call site) and asserts
the callee accepts each one. When you add a new kwarg to either side, this
test forces both sides to be updated together — preventing partial-merge drift
from reaching production.
"""
from __future__ import annotations

import inspect


def _accepted_params(fn) -> set[str]:
    sig = inspect.signature(fn)
    accepts_kwargs = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    if accepts_kwargs:
        # Function accepts **kwargs — anything goes
        return None  # type: ignore[return-value]
    return set(sig.parameters.keys())


# All kwargs run_agent._invoke_tool / parallel paths pass to handle_function_call.
# Update this set when the call site changes.
EXPECTED_HANDLE_FUNCTION_CALL_KWARGS = {
    "function_name",
    "function_args",
    "task_id",
    "enabled_tools",
    "honcho_manager",
    "honcho_session_key",
    "session_db",
}


def test_handle_function_call_accepts_all_invoke_tool_kwargs():
    from model_tools import handle_function_call

    accepted = _accepted_params(handle_function_call)
    if accepted is None:
        return  # **kwargs sink — nothing to check
    missing = EXPECTED_HANDLE_FUNCTION_CALL_KWARGS - accepted
    assert not missing, (
        f"handle_function_call is missing kwargs that run_agent._invoke_tool "
        f"passes: {missing}. Either add these params to handle_function_call "
        f"or remove the call site usage. (Production crash 2026-04-26 was "
        f"exactly this drift between commits.)"
    )


def test_mark_job_run_accepts_delivery_error():
    """cron/scheduler.py calls mark_job_run(..., delivery_error=de). Without
    that kwarg, every cron delivery failure crashes the tick loop."""
    from cron.jobs import mark_job_run

    params = _accepted_params(mark_job_run)
    if params is None:
        return
    assert "delivery_error" in params, (
        "mark_job_run must accept delivery_error= kwarg — scheduler.py calls "
        "with it. Production crash 2026-04-26 root cause was this drift."
    )
