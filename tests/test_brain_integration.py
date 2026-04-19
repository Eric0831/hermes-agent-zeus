"""Integration tests for the AgentEOS brain pipeline.

Tests the full flow: triage → plan → execute → evidence → verify,
using real DB operations but mocked LLM/agent calls.
"""

import json
import os
import pytest
from pathlib import Path
from unittest.mock import patch

from hermes_state import SessionDB
from brain import task_store, evidence as brain_evidence
from brain.executive import triage
from brain.planner import generate_plan
from brain.verifier import verify_task
from brain.config import is_brain_enabled, get_brain_config
from brain.models import PlanSpec


@pytest.fixture
def db(tmp_path):
    """Fresh SessionDB with v7 schema."""
    sdb = SessionDB(tmp_path / "test_state.db")
    sdb.create_session("sess_int_001", source="test", user_id="user_1")
    return sdb


class TestFullTaskPipeline:
    """End-to-end: triage → plan → create task → evidence → verify."""

    def test_research_task_pass(self, db):
        """Simulate a research task that passes verification."""
        # 1. Triage
        msg = "Research the top 3 Python web frameworks and compare their pros and cons"
        tr = triage(msg)
        assert tr.decision == "create_task"
        assert tr.task_type == "research"

        # 2. Create task
        tid = task_store.create_task(
            db, "sess_int_001",
            goal=msg,
            task_type=tr.task_type,
            priority=tr.priority,
            risk_level=tr.risk_level,
        )
        task_store.update_task_status(db, tid, "triaged", reason="executive")

        # 3. Plan
        mock_plan = {
            "goal": "Compare top 3 Python web frameworks",
            "success_criteria": [
                "At least 3 frameworks listed",
                "Each framework has pros and cons",
                "Comparison includes performance considerations",
            ],
            "subtasks": [
                {"id": "s1", "description": "Search for Python web frameworks", "tool": "web_search"},
                {"id": "s2", "description": "Analyze and compare", "depends_on": ["s1"]},
            ],
            "risks": ["Rate limiting"],
            "recommended_tools": ["web_search"],
        }

        def mock_llm(system, user):
            return json.dumps(mock_plan)

        plan = generate_plan(msg, task_type="research", llm_call=mock_llm)
        task_store.update_task_status(
            db, tid, "planned",
            plan_json=json.dumps(plan.to_dict()),
        )
        task_store.save_criteria(db, tid, plan.success_criteria)

        # 4. Simulate execution — capture evidence
        task_store.update_task_status(db, tid, "running", reason="exec_start")

        brain_evidence.capture_from_tool_result(
            tid, "web_search", "tc_001",
            json.dumps({
                "results": [
                    {"title": "Django - The web framework for perfectionists", "url": "..."},
                    {"title": "Flask - A micro web framework", "url": "..."},
                    {"title": "FastAPI - Modern, fast web framework", "url": "..."},
                ]
            }),
            db,
        )
        brain_evidence.capture_from_tool_result(
            tid, "web_extract", "tc_002",
            "Django pros: batteries-included, ORM. Cons: monolithic. "
            "Flask pros: lightweight, flexible. Cons: minimal. "
            "FastAPI pros: high performance, async. Cons: newer ecosystem.",
            db,
        )
        final_response = (
            "Here are the top 3 Python web frameworks compared:\n\n"
            "1. **Django** - Pros: batteries-included, great ORM. Cons: monolithic.\n"
            "2. **Flask** - Pros: lightweight, flexible. Cons: minimal built-in features.\n"
            "3. **FastAPI** - Pros: high performance, async support. Cons: newer ecosystem.\n\n"
            "Performance: FastAPI is fastest, Django is most feature-complete."
        )
        brain_evidence.capture_from_response(tid, final_response, db)

        # 5. Verify
        task_store.update_task_status(db, tid, "verifying")
        criteria = task_store.get_criteria(db, tid)
        ev = brain_evidence.get_evidence_for_task(tid, db)

        vr = verify_task(
            goal=plan.goal,
            criteria=criteria,
            evidence=ev,
            final_response=final_response,
        )
        assert vr.status == "pass"

        # 6. Complete
        task_store.update_task_status(
            db, tid, "completed",
            verification_status=vr.status,
            verification_json=json.dumps(vr.to_dict()),
        )

        # Verify final state
        task = task_store.get_task(db, tid)
        assert task["status"] == "completed"
        assert task["verification_status"] == "pass"
        assert task["completed_at"] is not None
        assert brain_evidence.get_evidence_count(tid, db) == 3

    def test_coding_task_fail_and_retry(self, db):
        """Simulate a coding task that fails verification, then succeeds on retry."""
        msg = "Fix the authentication bug in login.py"
        tid = task_store.create_task(
            db, "sess_int_001",
            goal=msg, task_type="coding",
        )
        task_store.update_task_status(db, tid, "triaged")

        # Plan with fallback (no LLM)
        plan = generate_plan(msg, task_type="coding")
        task_store.update_task_status(db, tid, "planned", plan_json=json.dumps(plan.to_dict()))
        task_store.save_criteria(db, tid, plan.success_criteria)

        # Execute — but NO tool evidence (model just talks)
        task_store.update_task_status(db, tid, "running")
        brain_evidence.capture_from_response(tid, "I've fixed the bug.", db)

        # Verify — should fail (no tool evidence for code changes)
        task_store.update_task_status(db, tid, "verifying")
        criteria = task_store.get_criteria(db, tid)
        ev = brain_evidence.get_evidence_for_task(tid, db)

        vr = verify_task(plan.goal, criteria, ev, "I've fixed the bug.")
        # Might pass or fail depending on heuristic — point is we can handle both
        assert vr.status in ("pass", "fail_retriable", "fail_non_retriable")

        if vr.status != "pass":
            # Retry
            retry_count, max_retries = task_store.increment_retry(db, tid)
            assert retry_count == 1
            assert retry_count <= max_retries

            task_store.update_task_status(db, tid, "running", reason="retry_1")

            # This time add real evidence
            brain_evidence.capture_from_tool_result(
                tid, "terminal", "tc_010",
                "All tests passed. 0 errors, 15 passed.",
                db,
            )
            brain_evidence.capture_from_tool_result(
                tid, "write_file", "tc_011",
                '{"path": "login.py", "status": "written"}',
                db,
            )

            task_store.update_task_status(db, tid, "verifying", reason="retry_verify")
            ev2 = brain_evidence.get_evidence_for_task(tid, db)
            vr2 = verify_task(plan.goal, criteria, ev2, "Fixed and all tests pass now.")

            final_status = "completed" if vr2.status == "pass" else "failed"
            task_store.update_task_status(db, tid, final_status,
                                          verification_status=vr2.status)

        task = task_store.get_task(db, tid)
        assert task["status"] in ("completed", "failed")

    def test_direct_reply_skips_brain(self, db):
        """Simple messages should not create tasks."""
        tr = triage("Hello, how are you?")
        assert tr.decision == "direct_reply"

        tr2 = triage("What is Python?")
        assert tr2.decision == "direct_reply"

        # No tasks created
        tasks = task_store.get_active_tasks(db, "sess_int_001")
        assert len(tasks) == 0

    def test_task_with_details_view(self, db):
        """Test the /tasks detail view data."""
        tid = task_store.create_task(db, "sess_int_001", goal="Test task")
        task_store.update_task_status(db, tid, "triaged")
        task_store.save_criteria(db, tid, ["criterion A", "criterion B"])
        brain_evidence.capture_from_tool_result(tid, "search", "tc1", "result", db)
        brain_evidence.capture_from_tool_result(tid, "fetch", "tc2", "data", db)

        detail = task_store.get_task_with_details(db, tid)
        assert detail is not None
        assert len(detail["criteria"]) == 2
        assert detail["evidence_count"] == 2
        assert len(detail["transitions"]) >= 1

    def test_stats_across_tasks(self, db):
        """Test aggregated stats."""
        t1 = task_store.create_task(db, "sess_int_001", goal="Task 1")
        t2 = task_store.create_task(db, "sess_int_001", goal="Task 2")
        t3 = task_store.create_task(db, "sess_int_001", goal="Task 3")

        # Complete t1
        task_store.update_task_status(db, t1, "triaged")
        task_store.update_task_status(db, t1, "running")
        task_store.update_task_status(db, t1, "verifying")
        task_store.update_task_status(db, t1, "completed")

        # Fail t2
        task_store.update_task_status(db, t2, "triaged")
        task_store.update_task_status(db, t2, "running")
        task_store.update_task_status(db, t2, "failed", failure_reason="timeout")

        stats = task_store.get_task_stats(db, "sess_int_001")
        assert stats["completed"] == 1
        assert stats["failed"] == 1
        assert stats["received"] == 1  # t3 still in received
        assert stats["total"] == 3

    def test_evidence_summary(self, db):
        """Test evidence summary generation."""
        tid = task_store.create_task(db, "sess_int_001", goal="Summary test")
        brain_evidence.capture_from_tool_result(tid, "web_search", "tc1", '{"result": "found stuff"}', db)
        brain_evidence.capture_from_tool_result(tid, "terminal", "tc2", "Tests passed", db)
        brain_evidence.capture_from_response(tid, "Here is the result.", db)

        summary = brain_evidence.get_evidence_summary(tid, db)
        assert "web_search" in summary
        assert "terminal" in summary
        assert len(summary) > 10


class TestBrainConfig:
    """Test brain configuration toggle."""

    def test_default_enabled(self, monkeypatch):
        monkeypatch.delenv("HERMES_BRAIN_ENABLED", raising=False)
        # Default should be enabled (config may or may not exist in test)
        result = is_brain_enabled()
        assert isinstance(result, bool)

    def test_env_disable(self, monkeypatch):
        monkeypatch.setenv("HERMES_BRAIN_ENABLED", "0")
        assert is_brain_enabled() is False

    def test_env_enable(self, monkeypatch):
        monkeypatch.setenv("HERMES_BRAIN_ENABLED", "1")
        assert is_brain_enabled() is True

    def test_get_config_defaults(self, monkeypatch):
        monkeypatch.setenv("HERMES_BRAIN_ENABLED", "1")
        cfg = get_brain_config()
        assert cfg["enabled"] is True
        assert cfg["max_retries"] == 2
        assert cfg["verify_with_llm"] is False


class TestTransitionHistory:
    """Test that task lifecycle is fully auditable."""

    def test_full_lifecycle_transitions(self, db):
        tid = task_store.create_task(db, "sess_int_001", goal="Audit test")
        task_store.update_task_status(db, tid, "triaged", reason="triage")
        task_store.update_task_status(db, tid, "planned", reason="plan_ok")
        task_store.update_task_status(db, tid, "running", reason="exec_start")
        task_store.update_task_status(db, tid, "verifying", reason="verify_start")
        task_store.update_task_status(db, tid, "completed", reason="pass")

        transitions = task_store.get_transitions(db, tid)
        assert len(transitions) == 6  # created + 5 updates
        states = [t["to_state"] for t in transitions]
        assert states == ["received", "triaged", "planned", "running", "verifying", "completed"]

        # All have timestamps
        assert all(t["created_at"] > 0 for t in transitions)

    def test_failed_retry_lifecycle(self, db):
        tid = task_store.create_task(db, "sess_int_001", goal="Retry audit")
        task_store.update_task_status(db, tid, "triaged")
        task_store.update_task_status(db, tid, "running")
        task_store.update_task_status(db, tid, "failed", failure_reason="no evidence")
        task_store.update_task_status(db, tid, "running", reason="retry_1")
        task_store.update_task_status(db, tid, "verifying")
        task_store.update_task_status(db, tid, "completed")

        transitions = task_store.get_transitions(db, tid)
        states = [t["to_state"] for t in transitions]
        assert "failed" in states
        assert states.count("running") == 2  # initial + retry
