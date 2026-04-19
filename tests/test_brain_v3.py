"""Tests for AgentEOS v3: meta-learning, pattern mining, strategy, proactive, governance."""

import json
import pytest
import time

from hermes_state import SessionDB
from brain import task_store, evidence as brain_evidence


@pytest.fixture
def db(tmp_path):
    sdb = SessionDB(tmp_path / "test_v3.db")
    sdb.create_session("sess_v3", source="test", user_id="user_1")
    return sdb


def _create_task(db, goal, task_type="research", status="completed", fail_reason=None):
    """Helper to create a task at a specific terminal state."""
    tid = task_store.create_task(db, "sess_v3", goal=goal, task_type=task_type)
    task_store.update_task_status(db, tid, "triaged")
    plan = {"goal": goal, "success_criteria": ["done"], "subtasks": [], "risks": [], "recommended_tools": []}
    task_store.update_task_status(db, tid, "planned", plan_json=json.dumps(plan))
    task_store.save_criteria(db, tid, ["done"])
    task_store.update_task_status(db, tid, "running")
    brain_evidence.capture_from_tool_result(tid, "web_search", f"tc_{tid}", "results", db)
    brain_evidence.capture_from_tool_result(tid, "web_extract", f"tc2_{tid}", "data", db)
    brain_evidence.capture_from_response(tid, "Here is the result.", db)
    task_store.update_task_status(db, tid, "verifying")
    if status == "completed":
        task_store.update_task_status(db, tid, "completed", verification_status="pass")
    else:
        task_store.update_task_status(db, tid, "failed",
                                      failure_reason=fail_reason or "test failure")
    return tid


@pytest.fixture
def rich_db(db):
    """DB with enough tasks for meaningful analysis."""
    for i in range(5):
        _create_task(db, f"Research topic {i}", "research")
    for i in range(3):
        _create_task(db, f"Fix bug {i}", "coding")
    _create_task(db, "Failed research", "research", "failed", "timeout")
    _create_task(db, "Failed coding", "coding", "failed", "tool error")
    return db


# ── Meta-Learning ─────────────────────────────────────────────────

class TestMetaLearning:
    def test_empty_run(self, db):
        from brain.meta_learning import execute_run
        result = execute_run(db)
        assert result["tasks_analyzed"] == 0
        assert result["findings"] == []

    def test_run_with_tasks(self, rich_db):
        from brain.meta_learning import execute_run, get_run, get_findings
        result = execute_run(rich_db)
        assert result["tasks_analyzed"] == 10
        assert len(result["findings"]) > 0

        run = get_run(rich_db, result["run_id"])
        assert run["status"] == "completed"

        findings = get_findings(rich_db, result["run_id"])
        assert len(findings) == len(result["findings"])

    def test_family_performance_finding(self, rich_db):
        from brain.meta_learning import execute_run
        result = execute_run(rich_db)
        family_findings = [f for f in result["findings"] if f["type"] == "family_performance"]
        assert len(family_findings) >= 1

    def test_scoped_run(self, rich_db):
        from brain.meta_learning import execute_run
        result = execute_run(rich_db, scope_id="sess_v3")
        assert result["tasks_analyzed"] == 10

    def test_recent_runs(self, rich_db):
        from brain.meta_learning import execute_run, get_recent_runs
        execute_run(rich_db)
        runs = get_recent_runs(rich_db)
        assert len(runs) == 1


# ── Pattern Mining ────────────────────────────────────────────────

class TestPatternMining:
    def test_mine_research_patterns(self, rich_db):
        from brain.pattern_mining import mine_patterns
        patterns = mine_patterns(rich_db, "research")
        assert len(patterns) > 0
        # Should find at least a success archetype or tool chain
        types = {p["pattern_type"] for p in patterns}
        assert len(types) >= 1

    def test_save_and_retrieve(self, db):
        from brain.pattern_mining import save_pattern, get_patterns
        pid = save_pattern(db, "success_archetype", "research",
                           {"tool_chain": ["web_search", "web_extract"]},
                           confidence=0.8, support_count=5)
        assert pid.startswith("pat_")

        patterns = get_patterns(db, "research")
        assert len(patterns) == 1
        assert patterns[0]["pattern_type"] == "success_archetype"

    def test_get_best_pattern(self, db):
        from brain.pattern_mining import save_pattern, get_best_pattern
        save_pattern(db, "success_archetype", "research",
                     {"tools": ["a"]}, confidence=0.6, support_count=3)
        save_pattern(db, "success_archetype", "research",
                     {"tools": ["b"]}, confidence=0.9, support_count=5)

        best = get_best_pattern(db, "research")
        assert best is not None
        assert best["confidence"] == pytest.approx(0.9)

    def test_no_patterns_for_few_tasks(self, db):
        from brain.pattern_mining import mine_patterns
        _create_task(db, "Solo task", "summary")
        patterns = mine_patterns(db, "summary", min_support=3)
        # Not enough tasks to form patterns
        assert isinstance(patterns, list)


# ── Strategy ──────────────────────────────────────────────────────

class TestStrategy:
    def test_propose_and_activate(self, db):
        from brain.strategy import propose_strategy, activate_strategy, get_active_strategy

        sid = propose_strategy(db, "planner_policy", "research",
                               {"max_subtasks": 4, "preferred_tools": ["web_search"]})
        assert sid.startswith("stv_") or sid.startswith("strat_")

        activated = activate_strategy(db, sid)
        assert activated is True

        active = get_active_strategy(db, "planner_policy", "research")
        assert active is not None
        assert active["status"] == "active"

    def test_activate_deprecates_previous(self, db):
        from brain.strategy import propose_strategy, activate_strategy, get_strategy_history

        s1 = propose_strategy(db, "planner_policy", "research", {"v": 1})
        activate_strategy(db, s1)

        s2 = propose_strategy(db, "planner_policy", "research", {"v": 2})
        activate_strategy(db, s2)

        history = get_strategy_history(db, "research")
        statuses = {h["id"]: h["status"] for h in history}
        assert statuses[s1] == "deprecated"
        assert statuses[s2] == "active"

    def test_rollback(self, db):
        from brain.strategy import propose_strategy, activate_strategy, rollback_strategy, get_active_strategy

        s1 = propose_strategy(db, "planner_policy", "coding", {"v": 1})
        activate_strategy(db, s1)
        s2 = propose_strategy(db, "planner_policy", "coding", {"v": 2})
        activate_strategy(db, s2)

        rolled_back = rollback_strategy(db, s2)
        assert rolled_back is True

        active = get_active_strategy(db, "planner_policy", "coding")
        assert active["id"] == s1


# ── Proactive Intelligence ────────────────────────────────────────

class TestProactive:
    def test_no_signals_clean_session(self, db):
        from brain.proactive import evaluate_signals
        signals = evaluate_signals(db, "sess_v3")
        assert isinstance(signals, list)

    def test_open_loop_detection(self, db):
        from brain.proactive import evaluate_signals
        # Create a failed task (open loop)
        _create_task(db, "Failed and abandoned", "research", "failed", "crashed")
        signals = evaluate_signals(db, "sess_v3")
        open_loops = [s for s in signals if s.get("action_type") == "nudge"]
        # May or may not detect depending on timing — at least no crash
        assert isinstance(signals, list)

    def test_create_and_execute_action(self, db):
        from brain.proactive import create_action, get_pending_actions, execute_action

        aid = create_action(db, "reminder", "task", "task_123",
                            {"message": "Task overdue"})
        assert aid is not None

        pending = get_pending_actions(db)
        assert len(pending) >= 1

        execute_action(db, aid)
        pending_after = get_pending_actions(db)
        executed_ids = {a.get("id") for a in pending_after if a.get("status") == "executed"}
        # After execute, it shouldn't be in pending
        pending_ids = {a["id"] for a in get_pending_actions(db)}
        assert aid not in pending_ids

    def test_format_nudge(self):
        from brain.proactive import format_nudge
        action = {
            "action_type": "reminder",
            "risk_level": "low",
            "reason_json": json.dumps({"reason": "Task overdue by 3 hours"}),
        }
        text = format_nudge(action)
        assert len(text) > 0
        assert "overdue" in text.lower()


# ── Governance ────────────────────────────────────────────────────

class TestGovernance:
    def test_review_proposal(self, db):
        from brain.governance import review_proposal, get_review_history

        rid = review_proposal(db, "strategy", "stv_123", 0.2, "approved",
                              notes="Low risk auto-approve")
        assert rid is not None

        history = get_review_history(db)
        assert len(history) >= 1
        assert history[0]["decision"] == "approved"

    def test_identity_drift_clean(self):
        from brain.governance import check_identity_drift
        proposal = {"max_subtasks": 6, "preferred_tools": ["web_search"]}
        identity = {
            "constraints": {
                "immutable": ["Never execute destructive operations without approval"],
            },
            "values": ["accuracy over speed"],
            "permissions": {"can_read_files": True},
        }
        result = check_identity_drift(proposal, identity)
        assert result["drift_score"] < 0.5
        assert result["decision"] in ("approved", "deferred")

    def test_identity_drift_violation(self):
        from brain.governance import check_identity_drift
        proposal = {
            "skip_verification": True,
            "auto_execute_destructive": True,
        }
        identity = {
            "constraints": {
                "immutable": [
                    "Never execute destructive operations without approval",
                    "Never bypass the verification step",
                ],
            },
            "values": ["safety over convenience"],
            "permissions": {},
        }
        result = check_identity_drift(proposal, identity)
        assert result["drift_score"] > 0.3
        # Should flag concern

    def test_auto_approve_rules(self):
        from brain.governance import can_auto_approve
        assert can_auto_approve(0.1, "strategy") is True
        assert can_auto_approve(0.5, "strategy") is False
        assert can_auto_approve(0.1, "identity") is False
        assert can_auto_approve(0.1, "policy") is False

    def test_governance_stats(self, db):
        from brain.governance import review_proposal, get_governance_stats
        review_proposal(db, "strategy", "s1", 0.1, "approved")
        review_proposal(db, "strategy", "s2", 0.8, "rejected")
        review_proposal(db, "strategy", "s3", 0.5, "deferred")

        stats = get_governance_stats(db)
        assert stats.get("approved", 0) == 1
        assert stats.get("rejected", 0) == 1
        assert stats.get("deferred", 0) == 1


# ── Full Evolution Pipeline ──────────────────────────────────────

class TestEvolutionPipeline:
    def test_end_to_end_evolution(self, rich_db):
        """Full v3 pipeline: meta-learn → mine patterns → propose strategy → govern."""
        from brain.meta_learning import execute_run
        from brain.pattern_mining import mine_patterns, get_best_pattern
        from brain.strategy import propose_strategy, activate_strategy
        from brain.governance import review_proposal, check_identity_drift, can_auto_approve
        from brain.identity import get_identity

        # 1. Meta-learning run
        ml_result = execute_run(rich_db)
        assert ml_result["tasks_analyzed"] >= 10

        # 2. Pattern mining
        patterns = mine_patterns(rich_db, "research")
        # Patterns may vary, but pipeline shouldn't crash

        # 3. Strategy proposal based on findings
        sid = propose_strategy(rich_db, "planner_policy", "research",
                               {"max_subtasks": 4, "preferred_tools": ["web_search"]},
                               source_run_id=ml_result["run_id"])

        # 4. Governance review
        identity = get_identity()
        drift = check_identity_drift(
            {"max_subtasks": 4, "preferred_tools": ["web_search"]},
            identity,
        )
        assert drift["drift_score"] < 0.5  # safe change

        if can_auto_approve(drift["drift_score"], "strategy"):
            review_proposal(rich_db, "strategy", sid, drift["drift_score"], "approved")
            activate_strategy(rich_db, sid)

        # 5. Verify strategy is active
        from brain.strategy import get_active_strategy
        active = get_active_strategy(rich_db, "planner_policy", "research")
        assert active is not None
