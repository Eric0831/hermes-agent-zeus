"""Tests for model_tools.py — function call dispatch, agent-loop interception, legacy toolsets."""

import json
import pytest
from unittest.mock import patch

from hermes_state import SessionDB
from model_tools import (
    handle_function_call,
    get_all_tool_names,
    get_toolset_for_tool,
    _AGENT_LOOP_TOOLS,
    _LEGACY_TOOLSET_MAP,
    TOOL_TO_TOOLSET_MAP,
)


# =========================================================================
# handle_function_call
# =========================================================================

class TestHandleFunctionCall:
    def test_agent_loop_tool_returns_error(self):
        for tool_name in _AGENT_LOOP_TOOLS:
            result = json.loads(handle_function_call(tool_name, {}))
            assert "error" in result
            assert "agent loop" in result["error"].lower()

    def test_unknown_tool_returns_error(self, tmp_path):
        db = SessionDB(tmp_path / "unknown_tool_policy.db")
        result = json.loads(
            handle_function_call(
                "totally_fake_tool_xyz",
                {},
                session_db=db,
            )
        )
        assert "error" in result
        assert "totally_fake_tool_xyz" in result["error"]

    def test_exception_returns_json_error(self, tmp_path):
        # Even if something goes wrong, should return valid JSON
        db = SessionDB(tmp_path / "exception_policy.db")
        result = handle_function_call(
            "web_search",
            None,
            session_db=db,
        )  # None args may cause issues
        parsed = json.loads(result)
        assert isinstance(parsed, dict)
        assert "error" in parsed
        assert len(parsed["error"]) > 0
        assert "error" in parsed["error"].lower() or "failed" in parsed["error"].lower()

    def test_policy_audit_uses_supplied_session_db(self, tmp_path):
        from brain import task_store

        db = SessionDB(tmp_path / "model_tools_policy.db")
        db.create_session("sess_policy", "test")
        task_id = task_store.create_task(
            db,
            "sess_policy",
            goal="Run a terminal check",
            task_type="coding",
            risk_level="medium",
        )

        with patch("model_tools.registry.dispatch", return_value="ok"):
            result = handle_function_call(
                "terminal",
                {"command": "echo ok"},
                task_id=task_id,
                session_db=db,
            )

        assert result == "ok"
        row = db._conn.execute(
            """SELECT task_id, target, risk_level, decision
               FROM policy_evaluations
               ORDER BY created_at DESC
               LIMIT 1"""
        ).fetchone()
        assert row is not None
        assert row["task_id"] == task_id
        assert row["target"] == "terminal"
        assert row["risk_level"] == "medium"


# =========================================================================
# Agent loop tools
# =========================================================================

class TestAgentLoopTools:
    def test_expected_tools_in_set(self):
        assert "todo" in _AGENT_LOOP_TOOLS
        assert "memory" in _AGENT_LOOP_TOOLS
        assert "session_search" in _AGENT_LOOP_TOOLS
        assert "delegate_task" in _AGENT_LOOP_TOOLS

    def test_no_regular_tools_in_set(self):
        assert "web_search" not in _AGENT_LOOP_TOOLS
        assert "terminal" not in _AGENT_LOOP_TOOLS


# =========================================================================
# Legacy toolset map
# =========================================================================

class TestLegacyToolsetMap:
    def test_expected_legacy_names(self):
        expected = [
            "web_tools", "terminal_tools", "vision_tools", "moa_tools",
            "image_tools", "skills_tools", "browser_tools", "cronjob_tools",
            "rl_tools", "file_tools", "tts_tools",
        ]
        for name in expected:
            assert name in _LEGACY_TOOLSET_MAP, f"Missing legacy toolset: {name}"

    def test_values_are_lists_of_strings(self):
        for name, tools in _LEGACY_TOOLSET_MAP.items():
            assert isinstance(tools, list), f"{name} is not a list"
            for tool in tools:
                assert isinstance(tool, str), f"{name} contains non-string: {tool}"


# =========================================================================
# Backward-compat wrappers
# =========================================================================

class TestBackwardCompat:
    def test_get_all_tool_names_returns_list(self):
        names = get_all_tool_names()
        assert isinstance(names, list)
        assert len(names) > 0
        # Should contain well-known tools
        assert "web_search" in names
        assert "terminal" in names

    def test_get_toolset_for_tool(self):
        result = get_toolset_for_tool("web_search")
        assert result is not None
        assert isinstance(result, str)

    def test_get_toolset_for_unknown_tool(self):
        result = get_toolset_for_tool("totally_nonexistent_tool")
        assert result is None

    def test_tool_to_toolset_map(self):
        assert isinstance(TOOL_TO_TOOLSET_MAP, dict)
        assert len(TOOL_TO_TOOLSET_MAP) > 0
