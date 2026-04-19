"""Tests for Phase 1 brain modules: world_state, policy, identity, metrics."""

import json
import os
import pytest
from pathlib import Path

from hermes_state import SessionDB
from brain import task_store, evidence as brain_evidence


@pytest.fixture
def db(tmp_path):
    sdb = SessionDB(tmp_path / "test_state.db")
    sdb.create_session("sess_p1", source="test", user_id="user_1")
    return sdb


@pytest.fixture
def populated_db(db):
    """DB with a mix of tasks in various states."""
    # Completed research task with evidence
    t1 = task_store.create_task(db, "sess_p1", goal="Research ORMs", task_type="research")
    task_store.update_task_status(db, t1, "triaged")
    task_store.save_criteria(db, t1, ["Found 3 ORMs", "Compared pros/cons"])
    task_store.update_task_status(db, t1, "planned", plan_json='{"goal":"Research ORMs","success_criteria":["Found 3 ORMs","Compared pros/cons"],"subtasks":[],"risks":[],"recommended_tools":[]}')
    task_store.update_task_status(db, t1, "running")
    brain_evidence.capture_from_tool_result(t1, "web_search", "tc1", '{"result":"Django, SQLAlchemy, Peewee"}', db)
    brain_evidence.capture_from_tool_result(t1, "web_extract", "tc2", "comparison data", db)
    brain_evidence.capture_from_response(t1, "Here are the ORMs compared...", db)
    task_store.update_task_status(db, t1, "verifying")
    task_store.update_task_status(db, t1, "completed", verification_status="pass")

    # Failed coding task
    t2 = task_store.create_task(db, "sess_p1", goal="Fix login bug", task_type="coding", risk_level="medium")
    task_store.update_task_status(db, t2, "triaged")
    task_store.update_task_status(db, t2, "running")
    task_store.update_task_status(db, t2, "failed", failure_reason="timeout")

    # Active summary task
    t3 = task_store.create_task(db, "sess_p1", goal="Summarize meeting", task_type="summary")
    task_store.update_task_status(db, t3, "triaged")
    task_store.update_task_status(db, t3, "running")

    # High-risk active task
    t4 = task_store.create_task(db, "sess_p1", goal="Deploy to prod", task_type="coding", risk_level="high")

    return db, {"t1": t1, "t2": t2, "t3": t3, "t4": t4}


# ── World State Tests ─────────────────────────────────────────────

class TestWorldState:
    def test_empty_session(self, db):
        from brain.world_state import get_world_state
        ws = get_world_state(db, "sess_p1")
        assert ws["active_tasks"] == []
        assert ws["completed_tasks_count"] == 0
        assert ws["session_health"] == "healthy"
        assert ws["computed_at"] > 0

    def test_populated_session(self, populated_db):
        from brain.world_state import get_world_state
        db, tids = populated_db
        ws = get_world_state(db, "sess_p1")

        assert ws["completed_tasks_count"] == 1
        assert ws["failed_tasks_count"] == 1
        assert len(ws["active_tasks"]) == 2  # t3 running + t4 received
        assert len(ws["tools_used"]) >= 2  # web_search, web_extract
        assert len(ws["recent_evidence"]) >= 3

    def test_risk_flags(self, populated_db):
        from brain.world_state import get_world_state
        db, tids = populated_db
        ws = get_world_state(db, "sess_p1")

        # Should flag: high-risk task active
        assert any("High-risk" in r for r in ws["risk_flags"])

    def test_open_loops(self, populated_db):
        from brain.world_state import get_world_state
        db, tids = populated_db
        ws = get_world_state(db, "sess_p1")

        assert len(ws["open_loops"]) >= 1  # t2 failed
        assert ws["open_loops"][0]["status"] == "failed"

    def test_health_degraded(self, populated_db):
        from brain.world_state import get_world_state
        db, tids = populated_db
        ws = get_world_state(db, "sess_p1")

        # Has risk flags → at least degraded
        assert ws["session_health"] in ("degraded", "failing")

    def test_summary_text(self, populated_db):
        from brain.world_state import get_world_state_summary
        db, _ = populated_db
        summary = get_world_state_summary(db, "sess_p1")
        assert len(summary) > 10
        assert "Active tasks" in summary or "Open loops" in summary or "Risk" in summary

    def test_none_db(self):
        from brain.world_state import get_world_state
        ws = get_world_state(None, "sess_none")
        assert ws["session_health"] == "healthy"


# ── Policy Tests ──────────────────────────────────────────────────

class TestPolicy:
    def test_low_risk_allows(self, db):
        from brain.policy import evaluate
        d = evaluate("tool_call", "web_search", db=db)
        assert d.decision == "allow"
        assert d.risk_level == "low"

    def test_high_risk_requires_approval(self, db):
        from brain.policy import evaluate
        d = evaluate("tool_call", "send_message", task_risk_level="high", db=db)
        assert d.decision == "allow_with_approval"
        assert d.risk_level == "high"

    def test_medium_shell_requires_approval(self, db):
        from brain.policy import evaluate
        d = evaluate("shell_exec", "terminal", task_risk_level="medium", db=db)
        assert d.decision == "allow_with_approval"

    def test_tool_call_convenience(self, db):
        from brain.policy import evaluate_tool_call
        d = evaluate_tool_call("read_file", db=db)
        assert d.decision == "allow"

    def test_audit_logging(self, db):
        from brain.policy import evaluate
        evaluate("tool_call", "web_search", task_id="task_test", db=db)
        evaluate("tool_call", "terminal", task_id="task_test", task_risk_level="high", db=db)

        rows = db._conn.execute(
            "SELECT * FROM policy_evaluations ORDER BY created_at"
        ).fetchall()
        assert len(rows) >= 2
        assert rows[0]["decision"] == "allow"

    def test_combined_risk(self):
        from brain.policy import _combine_risk
        assert _combine_risk("low", "low") == "low"
        assert _combine_risk("low", "high") == "high"
        assert _combine_risk("medium", "medium") == "medium"
        assert _combine_risk("high", "low") == "high"

    def test_none_db_no_crash(self):
        from brain.policy import evaluate
        d = evaluate("tool_call", "web_search", db=None)
        assert d.decision == "allow"


# ── Identity Tests ────────────────────────────────────────────────

class TestIdentity:
    def test_default_identity(self):
        from brain.identity import get_identity
        identity = get_identity()
        assert identity["name"] == "Hermes"
        assert "mission" in identity
        assert len(identity["values"]) > 0
        assert "immutable" in identity["constraints"]

    def test_identity_prompt(self):
        from brain.identity import get_identity_prompt
        prompt = get_identity_prompt()
        assert "IDENTITY" in prompt
        assert "Hermes" in prompt
        assert "Hard constraints" in prompt

    def test_immutable_constraints(self):
        from brain.identity import get_immutable_constraints
        constraints = get_immutable_constraints()
        assert len(constraints) >= 4
        assert any("destructive" in c.lower() for c in constraints)
        assert any("fabricate" in c.lower() for c in constraints)

    def test_permissions(self):
        from brain.identity import get_permissions, check_permission
        perms = get_permissions()
        assert perms["can_read_files"] is True
        assert perms["requires_approval_for_high_risk"] is True
        assert check_permission("can_read_files") is True

    def test_custom_identity_file(self, tmp_path, monkeypatch):
        """Test loading a custom identity.yaml."""
        import yaml
        custom = {
            "name": "CustomBot",
            "mission": "Custom mission",
            "values": ["custom value 1"],
        }
        identity_path = tmp_path / "hermes_test" / "identity.yaml"
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        with open(identity_path, "w") as f:
            yaml.dump(custom, f)

        from brain import identity as id_mod
        monkeypatch.setattr(id_mod, "_identity_path", lambda: identity_path)
        monkeypatch.setattr(id_mod, "_cached_identity", None)
        monkeypatch.setattr(id_mod, "_cached_mtime", 0.0)

        result = id_mod.get_identity()
        assert result["name"] == "CustomBot"
        assert result["mission"] == "Custom mission"
        # Should still have defaults merged in
        assert "constraints" in result


# ── Metrics Tests ─────────────────────────────────────────────────

class TestMetrics:
    def test_empty_metrics(self, db):
        from brain.metrics import get_brain_metrics, format_metrics_text
        m = get_brain_metrics(db, "sess_p1")
        assert m["computed_at"] > 0
        text = format_metrics_text(m)
        assert "no brain activity" in text.lower()

    def test_populated_metrics(self, populated_db):
        from brain.metrics import get_brain_metrics, format_metrics_text
        db, tids = populated_db
        m = get_brain_metrics(db, "sess_p1")

        assert m["tasks"]["total"] == 4
        assert m["tasks"]["completed"] == 1
        assert m["tasks"]["failed"] == 1
        assert m["tasks"]["completion_rate"] == 0.5

        assert m["verification"]["pass"] == 1

        assert m["evidence"]["total_records"] >= 3
        assert len(m["evidence"]["tools_used"]) >= 2

        text = format_metrics_text(m)
        assert "4 total" in text
        assert "50%" in text  # completion rate

    def test_none_db(self):
        from brain.metrics import get_brain_metrics
        m = get_brain_metrics(None, "sess_none")
        assert "computed_at" in m

    def test_global_metrics(self, populated_db):
        from brain.metrics import get_brain_metrics
        db, _ = populated_db
        m = get_brain_metrics(db)  # no session filter
        assert m["tasks"]["total"] == 4


# ── Task Recovery Tests ───────────────────────────────────────────

class TestTaskRecovery:
    def test_failed_task_can_be_resumed(self, populated_db):
        """Test that failed tasks can transition back to running."""
        db, tids = populated_db
        t2 = tids["t2"]

        task = task_store.get_task(db, t2)
        assert task["status"] == "failed"

        # Resume by transitioning back to running
        task_store.update_task_status(db, t2, "running", reason="manual_resume")
        task = task_store.get_task(db, t2)
        assert task["status"] == "running"

    def test_completed_task_cannot_resume(self, populated_db):
        db, tids = populated_db
        t1 = tids["t1"]

        with pytest.raises(ValueError, match="Invalid task transition"):
            task_store.update_task_status(db, t1, "running", reason="resume")

    def test_resume_preserves_evidence(self, populated_db):
        """Evidence from before the failure should still be accessible."""
        db, tids = populated_db
        t2 = tids["t2"]

        # Add some evidence before resume
        brain_evidence.capture_from_response(t2, "Partial work done", db)
        pre_count = brain_evidence.get_evidence_count(t2, db)

        # Resume
        task_store.update_task_status(db, t2, "running", reason="resume")

        # Add more evidence
        brain_evidence.capture_from_tool_result(t2, "terminal", "tc_r", "tests pass", db)
        post_count = brain_evidence.get_evidence_count(t2, db)

        assert post_count == pre_count + 1
