"""Tests for AgentEOS v4: evolution architect, capability manager, incubator, constitution, recursive reflection."""

import json
import pytest
import time

from hermes_state import SessionDB
from brain import task_store, evidence as brain_evidence


@pytest.fixture
def db(tmp_path):
    sdb = SessionDB(tmp_path / "test_v4.db")
    sdb.create_session("sess_v4", source="test", user_id="user_1")
    return sdb


def _create_task_full(db, goal, task_type="research", status="completed", fail_reason=None):
    """Create a task through the full lifecycle with evidence and reflection."""
    tid = task_store.create_task(db, "sess_v4", goal=goal, task_type=task_type)
    task_store.update_task_status(db, tid, "triaged")
    plan = {"goal": goal, "success_criteria": ["done"], "subtasks": [], "risks": []}
    task_store.update_task_status(db, tid, "planned", plan_json=json.dumps(plan))
    task_store.save_criteria(db, tid, ["done"])
    task_store.update_task_status(db, tid, "running")
    brain_evidence.capture_from_tool_result(tid, "web_search", f"tc_{tid}", "results", db)
    brain_evidence.capture_from_tool_result(tid, "web_extract", f"tc2_{tid}", "data", db)
    brain_evidence.capture_from_response(tid, "Result text.", db)
    task_store.update_task_status(db, tid, "verifying")
    if status == "completed":
        task_store.update_task_status(db, tid, "completed", verification_status="pass")
    else:
        task_store.update_task_status(db, tid, "failed", failure_reason=fail_reason or "test")
    # Generate reflection
    from brain.reflection import generate_reflection
    task = task_store.get_task(db, tid)
    ev = brain_evidence.get_evidence_for_task(tid, db)
    generate_reflection(db, task, ev)
    return tid


@pytest.fixture
def rich_db(db):
    """DB with enough data for v4 analysis."""
    for i in range(5):
        _create_task_full(db, f"Research topic {i}", "research")
    for i in range(3):
        _create_task_full(db, f"Fix bug {i}", "coding")
    _create_task_full(db, "Failed research", "research", "failed", "insufficient_evidence")
    _create_task_full(db, "Failed coding", "coding", "failed", "timeout")
    # Run meta-learning so findings exist
    from brain.meta_learning import execute_run
    execute_run(db)
    return db


# ══════════════════════════════════════════════════════════════════
# Evolution Architect Tests
# ══════════════════════════════════════════════════════════════════

class TestEvolutionArchitect:
    def test_no_proposals_empty_db(self, db):
        from brain.evolution_architect import generate_proposals
        pids = generate_proposals(db, "research")
        assert isinstance(pids, list)

    def test_generate_proposals(self, rich_db):
        from brain.evolution_architect import generate_proposals, get_proposal
        pids = generate_proposals(rich_db, "research")
        # May or may not generate depending on data patterns
        assert isinstance(pids, list)
        for pid in pids:
            p = get_proposal(rich_db, pid)
            assert p is not None
            assert p["status"] == "proposed"

    def test_get_proposals_filtered(self, rich_db):
        from brain.evolution_architect import generate_proposals, get_proposals
        generate_proposals(rich_db, "research")
        all_p = get_proposals(rich_db)
        assert isinstance(all_p, list)

    def test_update_status(self, rich_db):
        from brain.evolution_architect import generate_proposals, update_proposal_status, get_proposal
        pids = generate_proposals(rich_db, "research")
        if pids:
            update_proposal_status(rich_db, pids[0], "approved", "governance ok")
            p = get_proposal(rich_db, pids[0])
            assert p["status"] == "approved"


# ══════════════════════════════════════════════════════════════════
# Capability Manager Tests
# ══════════════════════════════════════════════════════════════════

class TestCapabilityManager:
    def test_create_version(self, db):
        from brain.capability_manager import create_version, get_version
        vid = create_version(db, "research_v2", {"new_verifier": True})
        assert vid is not None
        v = get_version(db, vid)
        assert v["status"] == "proposed"
        assert v["capability_family"] == "research_v2"

    def test_lifecycle_transitions(self, db):
        from brain.capability_manager import create_version, transition_status, get_version
        vid = create_version(db, "test_cap", {"x": 1})
        transition_status(db, vid, "incubating", "start incubation")
        transition_status(db, vid, "experimental", "incubator passed")
        transition_status(db, vid, "limited_rollout", "experiment ok")
        v = get_version(db, vid)
        assert v["status"] == "limited_rollout"

    def _walk_to_rollout(self, db, vid):
        """Walk a capability version through lifecycle to limited_rollout."""
        from brain.capability_manager import transition_status
        transition_status(db, vid, "incubating")
        transition_status(db, vid, "experimental")
        transition_status(db, vid, "limited_rollout")

    def test_adopt_deprecates_previous(self, db):
        from brain.capability_manager import create_version, adopt_version, get_version
        v1 = create_version(db, "cap_family", {"v": 1})
        self._walk_to_rollout(db, v1)
        adopt_version(db, v1)
        assert get_version(db, v1)["status"] == "adopted"

        v2 = create_version(db, "cap_family", {"v": 2})
        self._walk_to_rollout(db, v2)
        adopt_version(db, v2)
        assert get_version(db, v1)["status"] == "deprecated"
        assert get_version(db, v2)["status"] == "adopted"

    def test_get_active_version(self, db):
        from brain.capability_manager import create_version, adopt_version, get_active_version
        v1 = create_version(db, "my_cap", {"x": 1})
        self._walk_to_rollout(db, v1)
        adopt_version(db, v1)
        active = get_active_version(db, "my_cap")
        assert active is not None
        assert active["id"] == v1

    def test_family_history(self, db):
        from brain.capability_manager import create_version, get_family_history
        create_version(db, "hist_cap", {"v": 1})
        create_version(db, "hist_cap", {"v": 2})
        history = get_family_history(db, "hist_cap")
        assert len(history) == 2

    def test_stats(self, db):
        from brain.capability_manager import create_version, adopt_version, transition_status, get_capability_stats
        v1 = create_version(db, "s1", {"x": 1})
        self._walk_to_rollout(db, v1)
        adopt_version(db, v1)
        create_version(db, "s2", {"x": 2})
        stats = get_capability_stats(db)
        assert stats.get("adopted", 0) == 1
        assert stats.get("proposed", 0) == 1

    def test_extract_skill_infers_tool_risk(self, db):
        from brain.capability_manager import create_version, execute_action

        tid = task_store.create_task(db, "sess_v4", goal="Inspect runtime", task_type="general")
        task_store.update_task_status(db, tid, "triaged")
        task_store.update_task_status(
            db,
            tid,
            "planned",
            plan_json=json.dumps({
                "goal": "Inspect runtime",
                "success_criteria": ["checked"],
                "subtasks": [],
                "risks": [],
            }),
        )
        task_store.update_task_status(db, tid, "running")
        brain_evidence.capture_from_tool_result(tid, "terminal", "tc1", "ok", db)
        brain_evidence.capture_from_tool_result(tid, "patch", "tc2", "ok", db)
        task_store.update_task_status(db, tid, "verifying")
        task_store.update_task_status(db, tid, "completed", verification_status="pass")

        vid = create_version(db, "new_skill_family:general", {
            "source": {
                "action_hint": {
                    "kind": "extract_skill",
                    "task_family": "general",
                    "skill_name": "general_runtime_path",
                }
            }
        })
        result = execute_action(db, vid)

        assert result["executed"] is True
        assert result["result"]["risk_level"] == "medium"
        skill_id = result["result"]["skill_id"]
        row = db._conn.execute(
            "SELECT risk_level, status FROM skill_registry WHERE id = ?",
            (skill_id,),
        ).fetchone()
        assert row["risk_level"] == "medium"
        assert row["status"] == "candidate"


# ══════════════════════════════════════════════════════════════════
# Incubator Tests
# ══════════════════════════════════════════════════════════════════

class TestIncubator:
    def test_create_run(self, db):
        from brain.capability_manager import create_version
        from brain.incubator import create_run, get_run
        vid = create_version(db, "test_inc", {"x": 1})
        rid = create_run(db, vid, "replay", {"task_family": "research", "sample_size": 10})
        assert rid is not None
        run = get_run(db, rid)
        assert run["status"] == "pending"

    def test_evaluate_run(self, rich_db):
        from brain.capability_manager import create_version
        from brain.incubator import create_run, evaluate_run
        vid = create_version(rich_db, "eval_cap", {"x": 1})
        rid = create_run(rich_db, vid, "replay", {"task_family": "research"})
        result = evaluate_run(rich_db, rid)
        assert "recommendation" in result
        assert result["recommendation"] in ("promote", "revise", "discard")

    def test_complete_run(self, db):
        from brain.capability_manager import create_version
        from brain.incubator import create_run, complete_run, get_run
        vid = create_version(db, "comp_cap", {"x": 1})
        rid = create_run(db, vid, "simulation", {"scope": "test"})
        complete_run(db, rid, {"note": "test complete"})
        run = get_run(db, rid)
        assert run["status"] == "completed"


# ══════════════════════════════════════════════════════════════════
# Constitution Tests
# ══════════════════════════════════════════════════════════════════

class TestConstitution:
    def test_seed_defaults(self, db):
        from brain.constitution import seed_default_rules, load_constitution
        count = seed_default_rules(db)
        assert count >= 4
        rules = load_constitution(db)
        assert len(rules) >= 4
        types = {r["rule_type"] for r in rules}
        assert "immutable" in types
        assert "forbidden" in types

    def test_seed_idempotent(self, db):
        from brain.constitution import seed_default_rules
        c1 = seed_default_rules(db)
        c2 = seed_default_rules(db)
        assert c2 == 0  # no new rules on second call

    def test_evaluate_safe_proposal(self, db):
        from brain.constitution import seed_default_rules, evaluate_proposal
        seed_default_rules(db)
        result = evaluate_proposal(db, {"max_subtasks": 6, "preferred_tools": ["web_search"]})
        assert result["compliant"] is True
        assert result["decision"] == "allow"

    def test_evaluate_dangerous_proposal(self, db):
        from brain.constitution import seed_default_rules, evaluate_proposal
        seed_default_rules(db)
        result = evaluate_proposal(db, {
            "disable_verifier": True,
            "skip_governance": True,
        })
        assert result["compliant"] is False
        assert result["decision"] == "block"
        assert len(result["violations"]) > 0

    def test_add_custom_rule(self, db):
        from brain.constitution import add_rule, load_constitution
        rid = add_rule(db, "approval_required", "coding",
                       {"description": "All coding tasks need review"})
        assert rid is not None
        rules = load_constitution(db)
        assert any(r["id"] == rid for r in rules)


# ══════════════════════════════════════════════════════════════════
# Recursive Reflection Tests
# ══════════════════════════════════════════════════════════════════

class TestRecursiveReflection:
    def test_capability_reflection_empty(self, db):
        from brain.recursive_reflection import reflect_on_capability
        r = reflect_on_capability(db, "research")
        assert "effectiveness_score" in r
        assert isinstance(r.get("should_evolve"), bool)

    def test_capability_reflection_with_data(self, rich_db):
        from brain.recursive_reflection import reflect_on_capability
        r = reflect_on_capability(rich_db, "research")
        assert r["effectiveness_score"] >= 0
        assert isinstance(r["strengths"], list)
        assert isinstance(r["weaknesses"], list)

    def test_architecture_reflection(self, rich_db):
        from brain.recursive_reflection import reflect_on_architecture
        r = reflect_on_architecture(rich_db)
        assert isinstance(r.get("healthy_families"), list)
        assert isinstance(r.get("struggling_families"), list)

    def test_save_and_get(self, db):
        from brain.recursive_reflection import save_reflection, get_reflections
        rid = save_reflection(db, "capability", "research", "capability",
                              {"note": "test reflection"}, confidence=0.8)
        assert rid is not None
        refls = get_reflections(db, scope_type="capability", level="capability")
        assert len(refls) >= 1

    def test_architecture_reflection_empty(self, db):
        from brain.recursive_reflection import reflect_on_architecture
        r = reflect_on_architecture(db)
        assert isinstance(r, dict)


# ══════════════════════════════════════════════════════════════════
# Full Evolution Pipeline
# ══════════════════════════════════════════════════════════════════

class TestEvolutionPipeline:
    def test_full_v4_pipeline(self, rich_db):
        """End-to-end: analyze → propose → constitution check → incubate → evaluate."""
        from brain.evolution_architect import generate_proposals, get_proposal
        from brain.constitution import seed_default_rules, evaluate_proposal
        from brain.capability_manager import create_version, adopt_version, get_active_version
        from brain.incubator import create_run, evaluate_run
        from brain.recursive_reflection import reflect_on_capability

        # 1. Seed constitution
        seed_default_rules(rich_db)

        # 2. Generate proposals
        pids = generate_proposals(rich_db, "research")

        # 3. For each proposal: constitution check → create capability → incubate
        adopted_count = 0
        for pid in pids[:2]:
            p = get_proposal(rich_db, pid)
            if not p:
                continue

            # Constitution check
            proposal_def = json.loads(p["proposal_json"]) if isinstance(p["proposal_json"], str) else p["proposal_json"]
            const_result = evaluate_proposal(rich_db, proposal_def)

            if const_result["decision"] == "block":
                continue  # skip blocked proposals

            # Create capability version
            vid = create_version(rich_db, f"research_evolved_{pid[-4:]}",
                                 proposal_def, source_proposal_id=pid)

            # Incubate
            iid = create_run(rich_db, vid, "replay", {"task_family": "research"})
            eval_result = evaluate_run(rich_db, iid)

            if eval_result.get("recommendation") == "promote":
                adopt_version(rich_db, vid)
                adopted_count += 1

        # 4. Reflect on the capability family
        refl = reflect_on_capability(rich_db, "research")
        assert "effectiveness_score" in refl

        # Pipeline completed without errors
        assert isinstance(adopted_count, int)
