"""Tests for brain.task_store — CRUD, state machine, criteria, evidence."""

import json
import pytest
import sqlite3
import tempfile
from pathlib import Path

from hermes_state import SessionDB
from brain import task_store, evidence
from brain.models import PlanSpec, SubtaskSpec


@pytest.fixture
def db(tmp_path):
    """Create a fresh SessionDB with v7 schema for testing."""
    db_path = tmp_path / "test_state.db"
    sdb = SessionDB(db_path)
    # Create a session to satisfy FK constraints
    sdb.create_session("sess_001", source="test", user_id="user_1")
    return sdb


class TestCreateTask:
    def test_basic_create(self, db):
        tid = task_store.create_task(db, "sess_001", goal="Test goal")
        assert tid.startswith("task_")

        task = task_store.get_task(db, tid)
        assert task is not None
        assert task["goal"] == "Test goal"
        assert task["status"] == "received"
        assert task["task_type"] == "general"
        assert task["priority"] == "medium"
        assert task["risk_level"] == "low"
        assert task["retry_count"] == 0

    def test_create_with_options(self, db):
        tid = task_store.create_task(
            db, "sess_001",
            goal="Research task",
            task_type="research",
            priority="high",
            risk_level="medium",
            requires_approval=True,
            budget_tokens=10000,
        )
        task = task_store.get_task(db, tid)
        assert task["task_type"] == "research"
        assert task["priority"] == "high"
        assert task["risk_level"] == "medium"
        assert task["requires_approval"] == 1
        assert task["budget_tokens"] == 10000

    def test_create_logs_transition(self, db):
        tid = task_store.create_task(db, "sess_001", goal="Test")
        transitions = task_store.get_transitions(db, tid)
        assert len(transitions) == 1
        assert transitions[0]["from_state"] == "none"
        assert transitions[0]["to_state"] == "received"
        assert transitions[0]["reason"] == "task_created"


class TestStateMachine:
    def test_valid_transitions(self, db):
        tid = task_store.create_task(db, "sess_001", goal="Test")
        task_store.update_task_status(db, tid, "triaged", reason="triage_done")
        task_store.update_task_status(db, tid, "planned", reason="plan_generated")
        task_store.update_task_status(db, tid, "running", reason="exec_started")
        task_store.update_task_status(db, tid, "verifying", reason="exec_done")
        task_store.update_task_status(db, tid, "completed", reason="verified")

        task = task_store.get_task(db, tid)
        assert task["status"] == "completed"
        assert task["completed_at"] is not None
        assert task["started_at"] is not None

    def test_invalid_transition_raises(self, db):
        tid = task_store.create_task(db, "sess_001", goal="Test")
        with pytest.raises(ValueError, match="Invalid task transition"):
            task_store.update_task_status(db, tid, "completed")

    def test_completed_is_terminal(self, db):
        tid = task_store.create_task(db, "sess_001", goal="Test")
        task_store.update_task_status(db, tid, "triaged")
        task_store.update_task_status(db, tid, "running")
        task_store.update_task_status(db, tid, "verifying")
        task_store.update_task_status(db, tid, "completed")
        with pytest.raises(ValueError, match="Invalid task transition"):
            task_store.update_task_status(db, tid, "running")

    def test_failed_allows_retry(self, db):
        tid = task_store.create_task(db, "sess_001", goal="Test")
        task_store.update_task_status(db, tid, "triaged")
        task_store.update_task_status(db, tid, "running")
        task_store.update_task_status(db, tid, "failed", failure_reason="timeout")
        # Retry: failed -> running
        task_store.update_task_status(db, tid, "running", reason="retry")
        task = task_store.get_task(db, tid)
        assert task["status"] == "running"
        assert task["failure_reason"] == "timeout"

    def test_plan_json_stored(self, db):
        tid = task_store.create_task(db, "sess_001", goal="Test")
        task_store.update_task_status(db, tid, "triaged")

        plan = PlanSpec(
            goal="Test goal",
            success_criteria=["criterion A", "criterion B"],
            subtasks=[SubtaskSpec(id="s1", description="step 1")],
        )
        task_store.update_task_status(
            db, tid, "planned",
            plan_json=json.dumps(plan.to_dict()),
        )
        task = task_store.get_task(db, tid)
        loaded = json.loads(task["plan_json"])
        assert loaded["goal"] == "Test goal"
        assert len(loaded["success_criteria"]) == 2

    def test_verification_stored(self, db):
        tid = task_store.create_task(db, "sess_001", goal="Test")
        task_store.update_task_status(db, tid, "triaged")
        task_store.update_task_status(db, tid, "running")
        task_store.update_task_status(
            db, tid, "verifying",
            verification_status="pass",
            verification_json='{"status":"pass"}',
        )
        task = task_store.get_task(db, tid)
        assert task["verification_status"] == "pass"

    def test_transitions_logged(self, db):
        tid = task_store.create_task(db, "sess_001", goal="Test")
        task_store.update_task_status(db, tid, "triaged", reason="r1")
        task_store.update_task_status(db, tid, "planned", reason="r2")
        task_store.update_task_status(db, tid, "running", reason="r3")

        transitions = task_store.get_transitions(db, tid)
        assert len(transitions) == 4  # created + 3 updates
        assert [t["to_state"] for t in transitions] == [
            "received", "triaged", "planned", "running"
        ]


class TestTaskQueries:
    def test_get_active_tasks(self, db):
        t1 = task_store.create_task(db, "sess_001", goal="Active 1")
        t2 = task_store.create_task(db, "sess_001", goal="Active 2")
        t3 = task_store.create_task(db, "sess_001", goal="Done")
        task_store.update_task_status(db, t3, "triaged")
        task_store.update_task_status(db, t3, "running")
        task_store.update_task_status(db, t3, "verifying")
        task_store.update_task_status(db, t3, "completed")

        active = task_store.get_active_tasks(db, "sess_001")
        active_ids = {t["id"] for t in active}
        assert t1 in active_ids
        assert t2 in active_ids
        assert t3 not in active_ids

    def test_get_session_tasks(self, db):
        task_store.create_task(db, "sess_001", goal="Task A")
        task_store.create_task(db, "sess_001", goal="Task B")
        tasks = task_store.get_session_tasks(db, "sess_001")
        assert len(tasks) == 2

    def test_get_nonexistent_task(self, db):
        assert task_store.get_task(db, "task_nonexistent") is None

    def test_task_with_details(self, db):
        tid = task_store.create_task(db, "sess_001", goal="Detail test")
        task_store.save_criteria(db, tid, ["crit A", "crit B"])

        detail = task_store.get_task_with_details(db, tid)
        assert detail is not None
        assert len(detail["criteria"]) == 2
        assert len(detail["transitions"]) == 1
        assert detail["evidence_count"] == 0


class TestCriteria:
    def test_save_and_get(self, db):
        tid = task_store.create_task(db, "sess_001", goal="Test")
        task_store.save_criteria(db, tid, ["A done", "B done", "C done"])

        criteria = task_store.get_criteria(db, tid)
        assert len(criteria) == 3
        assert criteria[0]["criterion_key"] == "c0"
        assert criteria[0]["description"] == "A done"
        assert criteria[0]["status"] == "pending"

    def test_update_criterion(self, db):
        tid = task_store.create_task(db, "sess_001", goal="Test")
        task_store.save_criteria(db, tid, ["X done"])
        task_store.update_criterion(db, tid, "c0", "met", evidence_ids=["ev_1"])

        criteria = task_store.get_criteria(db, tid)
        assert criteria[0]["status"] == "met"
        assert json.loads(criteria[0]["evidence_ids"]) == ["ev_1"]


class TestRetry:
    def test_increment(self, db):
        tid = task_store.create_task(db, "sess_001", goal="Retry test")
        count, max_r = task_store.increment_retry(db, tid)
        assert count == 1
        assert max_r == 2

        count2, _ = task_store.increment_retry(db, tid)
        assert count2 == 2

    def test_nonexistent_raises(self, db):
        with pytest.raises(ValueError, match="Task not found"):
            task_store.increment_retry(db, "task_nope")


class TestStats:
    def test_empty(self, db):
        stats = task_store.get_task_stats(db, "sess_001")
        assert stats["total"] == 0

    def test_with_tasks(self, db):
        t1 = task_store.create_task(db, "sess_001", goal="A")
        t2 = task_store.create_task(db, "sess_001", goal="B")
        task_store.update_task_status(db, t2, "triaged")
        task_store.update_task_status(db, t2, "running")
        task_store.update_task_status(db, t2, "verifying")
        task_store.update_task_status(db, t2, "completed")

        stats = task_store.get_task_stats(db, "sess_001")
        assert stats["received"] == 1
        assert stats["completed"] == 1
        assert stats["total"] == 2


class TestEvidence:
    def test_capture_tool_result(self, db):
        tid = task_store.create_task(db, "sess_001", goal="Evidence test")
        eid = evidence.capture_from_tool_result(
            tid, "web_search", "tc_001",
            '{"result": "found 3 items"}', db,
        )
        assert eid.startswith("ev_")

        records = evidence.get_evidence_for_task(tid, db)
        assert len(records) == 1
        assert records[0]["source_type"] == "tool_output"
        assert records[0]["tool_name"] == "web_search"
        assert "found 3 items" in records[0]["summary"]

    def test_capture_response(self, db):
        tid = task_store.create_task(db, "sess_001", goal="Response test")
        eid = evidence.capture_from_response(tid, "Here is the answer...", db)

        records = evidence.get_evidence_for_task(tid, db)
        assert len(records) == 1
        assert records[0]["source_type"] == "llm_response"

    def test_multiple_evidence(self, db):
        tid = task_store.create_task(db, "sess_001", goal="Multi evidence")
        evidence.capture_from_tool_result(tid, "search", "tc1", "result 1", db)
        evidence.capture_from_tool_result(tid, "fetch", "tc2", "result 2", db)
        evidence.capture_from_response(tid, "final answer", db)

        assert evidence.get_evidence_count(tid, db) == 3

        summary = evidence.get_evidence_summary(tid, db)
        assert "search" in summary
        assert "fetch" in summary

    def test_capture_custom(self, db):
        tid = task_store.create_task(db, "sess_001", goal="Custom evidence")
        eid = evidence.capture_custom(
            tid, "test_result", "pytest_run_1",
            "All 15 tests passed",
            {"passed": 15, "failed": 0},
            db,
        )
        records = evidence.get_evidence_for_task(tid, db)
        assert len(records) == 1
        assert records[0]["source_type"] == "test_result"

    def test_empty_output(self, db):
        tid = task_store.create_task(db, "sess_001", goal="Empty")
        evidence.capture_from_tool_result(tid, "noop", "tc1", "", db)
        records = evidence.get_evidence_for_task(tid, db)
        assert "empty output" in records[0]["summary"]

    def test_large_payload_truncated(self, db):
        tid = task_store.create_task(db, "sess_001", goal="Large")
        big_output = "x" * 20000
        evidence.capture_from_tool_result(tid, "big", "tc1", big_output, db)
        records = evidence.get_evidence_for_task(tid, db)
        assert len(records[0]["payload_json"]) <= 10001  # 10000 + newline


class TestModels:
    def test_plan_spec_roundtrip(self):
        plan = PlanSpec(
            goal="Test",
            success_criteria=["A", "B"],
            subtasks=[
                SubtaskSpec(id="s1", description="Step 1", tool="search"),
                SubtaskSpec(id="s2", description="Step 2", depends_on=["s1"]),
            ],
            risks=["might fail"],
        )
        d = plan.to_dict()
        restored = PlanSpec.from_dict(d)
        assert restored.goal == "Test"
        assert len(restored.subtasks) == 2
        assert restored.subtasks[1].depends_on == ["s1"]
