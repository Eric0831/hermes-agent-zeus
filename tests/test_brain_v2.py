"""Tests for AgentEOS v2 modules: memory, curator, skill_engine, reflection."""

import json
import pytest
import time

from hermes_state import SessionDB
from brain import task_store, evidence as brain_evidence


@pytest.fixture
def db(tmp_path):
    sdb = SessionDB(tmp_path / "test_v2.db")
    sdb.create_session("sess_v2", source="test", user_id="user_1")
    return sdb


def _create_completed_task(db, goal="Research Python ORMs", task_type="research"):
    """Helper: create a task and run it through the full lifecycle."""
    tid = task_store.create_task(db, "sess_v2", goal=goal, task_type=task_type)
    task_store.update_task_status(db, tid, "triaged")

    plan_json = json.dumps({
        "goal": goal,
        "success_criteria": ["Found frameworks", "Compared features"],
        "subtasks": [
            {"id": "s1", "description": "Search", "tool": "web_search"},
            {"id": "s2", "description": "Analyze", "depends_on": ["s1"]},
        ],
        "risks": ["Rate limiting"],
        "recommended_tools": ["web_search"],
    })
    task_store.update_task_status(db, tid, "planned", plan_json=plan_json)
    task_store.save_criteria(db, tid, ["Found frameworks", "Compared features"])
    task_store.update_task_status(db, tid, "running")

    brain_evidence.capture_from_tool_result(tid, "web_search", "tc1", '{"results":["Django","Flask"]}', db)
    brain_evidence.capture_from_tool_result(tid, "web_extract", "tc2", "Django: full-stack. Flask: micro.", db)
    brain_evidence.capture_from_response(tid, "Here are the ORMs compared...", db)

    task_store.update_task_status(db, tid, "verifying")
    task_store.update_task_status(db, tid, "completed",
                                  verification_status="pass",
                                  verification_json='{"status":"pass","criteria_results":[]}')
    return tid


# ══════════════════════════════════════════════════════════════════
# Memory Tests
# ══════════════════════════════════════════════════════════════════

class TestLayeredMemory:
    def test_write_and_retrieve(self, db):
        from brain.memory import write_memory, retrieve

        mid = write_memory(db, "episodic", "sess_v2",
                           {"event": "completed research task"},
                           title="Research task done")
        assert mid.startswith("mem_")

        records = retrieve(db, "sess_v2")
        assert len(records) == 1
        assert records[0]["memory_type"] == "episodic"
        assert records[0]["title"] == "Research task done"

    def test_filter_by_type(self, db):
        from brain.memory import write_memory, retrieve

        write_memory(db, "episodic", "sess_v2", {"x": 1}, title="ep1")
        write_memory(db, "semantic", "sess_v2", {"x": 2}, title="sem1")
        write_memory(db, "profile", "sess_v2", {"x": 3}, title="prof1")

        ep = retrieve(db, "sess_v2", memory_types=["episodic"])
        assert len(ep) == 1
        assert ep[0]["memory_type"] == "episodic"

        sem = retrieve(db, "sess_v2", memory_types=["semantic"])
        assert len(sem) == 1

    def test_supersession(self, db):
        from brain.memory import write_memory, retrieve, get_memory

        old_id = write_memory(db, "semantic", "sess_v2", {"v": 1}, title="old")
        new_id = write_memory(db, "semantic", "sess_v2", {"v": 2}, title="new",
                              supersedes_id=old_id)

        old = get_memory(db, old_id)
        assert old["is_active"] == 0

        active = retrieve(db, "sess_v2", memory_types=["semantic"])
        assert len(active) == 1
        assert active[0]["id"] == new_id

    def test_freshness_decay(self, db):
        from brain.memory import _compute_freshness
        assert _compute_freshness(0) == pytest.approx(1.0)
        assert _compute_freshness(14) == pytest.approx(0.5, abs=0.01)
        assert _compute_freshness(28) == pytest.approx(0.25, abs=0.01)

    def test_confidence_update(self, db):
        from brain.memory import write_memory, update_confidence, get_memory
        mid = write_memory(db, "episodic", "sess_v2", {"x": 1}, confidence=0.5)
        update_confidence(db, mid, 0.95)
        m = get_memory(db, mid)
        assert m["confidence"] == pytest.approx(0.95)

    def test_deactivate(self, db):
        from brain.memory import write_memory, deactivate, retrieve
        mid = write_memory(db, "episodic", "sess_v2", {"x": 1})
        deactivate(db, mid)
        active = retrieve(db, "sess_v2")
        assert len(active) == 0

    def test_stats(self, db):
        from brain.memory import write_memory, get_memory_stats
        write_memory(db, "episodic", "sess_v2", {"x": 1})
        write_memory(db, "episodic", "sess_v2", {"x": 2})
        write_memory(db, "semantic", "sess_v2", {"x": 3})
        stats = get_memory_stats(db, "sess_v2")
        assert stats["episodic"] == 2
        assert stats["semantic"] == 1
        assert stats["total"] == 3

    def test_query_filter(self, db):
        from brain.memory import write_memory, retrieve
        write_memory(db, "episodic", "sess_v2", {"topic": "python ORM"}, title="Python ORM research")
        write_memory(db, "episodic", "sess_v2", {"topic": "weather API"}, title="Weather API integration")

        results = retrieve(db, "sess_v2", query="python")
        assert len(results) == 1
        assert "Python" in results[0]["title"]


# ══════════════════════════════════════════════════════════════════
# Memory Curator Tests
# ══════════════════════════════════════════════════════════════════

class TestMemoryCurator:
    def test_curate_successful_task(self, db):
        from brain.memory_curator import curate_after_task
        from brain.memory import get_memory_stats

        tid = _create_completed_task(db)
        task = task_store.get_task(db, tid)
        ev = brain_evidence.get_evidence_for_task(tid, db)
        plan = json.loads(task["plan_json"])

        result = curate_after_task(db, tid, task, ev, "pass", plan)

        assert result["episodic_written"] is not None
        assert result["semantic_written"] is not None
        assert result["skill_candidate"] is True

        stats = get_memory_stats(db, "sess_v2")
        assert stats["episodic"] >= 1
        assert stats["semantic"] >= 1

    def test_curate_failed_task(self, db):
        from brain.memory_curator import curate_after_task

        tid = task_store.create_task(db, "sess_v2", goal="Failing task")
        task_store.update_task_status(db, tid, "triaged")
        task_store.update_task_status(db, tid, "running")
        task_store.update_task_status(db, tid, "failed", failure_reason="timeout")

        task = task_store.get_task(db, tid)
        result = curate_after_task(db, tid, task, [], "fail_retriable")

        assert result["episodic_written"] is not None
        assert result["semantic_written"] is None  # failed tasks don't generate semantic
        assert result["skill_candidate"] is False

    def test_conflict_detection(self, db):
        from brain.memory import write_memory
        from brain.memory_curator import detect_conflicts

        write_memory(db, "semantic", "sess_v2", {"v": 1}, title="Pattern: research task with web_search")
        write_memory(db, "semantic", "sess_v2", {"v": 2}, title="Pattern: research task with web_extract")

        conflicts = detect_conflicts(db, "sess_v2", "semantic")
        assert len(conflicts) >= 1


# ══════════════════════════════════════════════════════════════════
# Skill Engine Tests
# ══════════════════════════════════════════════════════════════════

class TestSkillEngine:
    def test_generate_candidate(self, db):
        from brain.skill_engine import generate_candidate, get_skill

        tid = _create_completed_task(db)
        task = task_store.get_task(db, tid)
        plan = json.loads(task["plan_json"])
        ev = brain_evidence.get_evidence_for_task(tid, db)

        skill_id = generate_candidate(db, task, plan, ev)
        assert skill_id is not None
        assert skill_id.startswith("skill_")

        skill = get_skill(db, skill_id)
        assert skill["status"] == "candidate"
        assert skill["intent_family"] == "research_general"

    def test_auto_promote_low_risk(self, db):
        from brain.skill_engine import generate_candidate, auto_promote, get_skill

        tid = _create_completed_task(db)
        task = task_store.get_task(db, tid)
        plan = json.loads(task["plan_json"])
        ev = brain_evidence.get_evidence_for_task(tid, db)

        skill_id = generate_candidate(db, task, plan, ev)
        promoted = auto_promote(db, skill_id)
        assert promoted is True

        skill = get_skill(db, skill_id)
        assert skill["status"] == "active"

    def test_auto_promote_rejects_medium_risk(self, db):
        from brain.skill_engine import generate_candidate, auto_promote, get_skill

        tid = _create_completed_task(db)
        task = task_store.get_task(db, tid)
        plan = json.loads(task["plan_json"])
        ev = brain_evidence.get_evidence_for_task(tid, db)

        skill_id = generate_candidate(db, task, plan, ev)
        db._execute_write(lambda conn: conn.execute(
            "UPDATE skill_registry SET risk_level = 'medium' WHERE id = ?",
            (skill_id,),
        ))

        promoted = auto_promote(db, skill_id)
        assert promoted is False
        assert get_skill(db, skill_id)["status"] == "candidate"

    def test_search_skills(self, db):
        from brain.skill_engine import generate_candidate, auto_promote, search_skills

        tid = _create_completed_task(db)
        task = task_store.get_task(db, tid)
        plan = json.loads(task["plan_json"])
        ev = brain_evidence.get_evidence_for_task(tid, db)

        skill_id = generate_candidate(db, task, plan, ev)
        auto_promote(db, skill_id)

        results = search_skills(db, intent_family="research_general")
        assert len(results) == 1
        assert results[0]["id"] == skill_id

    def test_record_application(self, db):
        from brain.skill_engine import (
            generate_candidate, auto_promote, record_application,
            update_application, update_success_rate, get_skill,
        )

        tid = _create_completed_task(db)
        task = task_store.get_task(db, tid)
        plan = json.loads(task["plan_json"])
        ev = brain_evidence.get_evidence_for_task(tid, db)

        skill_id = generate_candidate(db, task, plan, ev)
        auto_promote(db, skill_id)

        app_id = record_application(db, "task_new", skill_id)
        update_application(db, app_id, "succeeded", "Worked well")

        skill = get_skill(db, skill_id)
        assert skill["usage_count"] == 1

        rate = update_success_rate(db, skill_id)
        assert rate == 1.0

    def test_deprecate(self, db):
        from brain.skill_engine import generate_candidate, deprecate_skill, get_skill

        tid = _create_completed_task(db)
        task = task_store.get_task(db, tid)
        plan = json.loads(task["plan_json"])
        ev = brain_evidence.get_evidence_for_task(tid, db)

        skill_id = generate_candidate(db, task, plan, ev)
        deprecate_skill(db, skill_id, "outdated")
        assert get_skill(db, skill_id)["status"] == "deprecated"

    def test_stats(self, db):
        from brain.skill_engine import generate_candidate, auto_promote, get_skill_stats

        tid = _create_completed_task(db)
        task = task_store.get_task(db, tid)
        plan = json.loads(task["plan_json"])
        ev = brain_evidence.get_evidence_for_task(tid, db)

        skill_id = generate_candidate(db, task, plan, ev)
        auto_promote(db, skill_id)

        stats = get_skill_stats(db)
        assert stats.get("active", 0) == 1

    def test_no_candidate_for_incomplete(self, db):
        from brain.skill_engine import generate_candidate

        tid = task_store.create_task(db, "sess_v2", goal="Incomplete")
        task = task_store.get_task(db, tid)
        # status is "received", not "completed"
        assert generate_candidate(db, task, {}, []) is None


# ══════════════════════════════════════════════════════════════════
# Reflection Tests
# ══════════════════════════════════════════════════════════════════

class TestReflection:
    def test_success_reflection(self, db):
        from brain.reflection import generate_reflection

        tid = _create_completed_task(db)
        task = task_store.get_task(db, tid)
        ev = brain_evidence.get_evidence_for_task(tid, db)

        refl = generate_reflection(db, task, ev)
        assert refl["root_cause_class"] == "success"
        assert refl["outcome"] == "completed"
        assert len(refl["what_worked"]) > 0
        assert refl["confidence"] > 0.5
        assert refl["id"].startswith("refl_")

    def test_failure_reflection(self, db):
        from brain.reflection import generate_reflection

        tid = task_store.create_task(db, "sess_v2", goal="Failed task")
        task_store.update_task_status(db, tid, "triaged")
        task_store.update_task_status(db, tid, "running")
        task_store.update_task_status(db, tid, "failed", failure_reason="timeout exceeded")
        task = task_store.get_task(db, tid)

        refl = generate_reflection(db, task, [])
        assert refl["root_cause_class"] == "timeout"
        assert len(refl["what_failed"]) > 0

    def test_policy_deltas(self, db):
        from brain.reflection import generate_reflection

        tid = task_store.create_task(db, "sess_v2", goal="Timed out task")
        task_store.update_task_status(db, tid, "triaged")
        task_store.update_task_status(db, tid, "running")
        task_store.update_task_status(db, tid, "failed", failure_reason="timeout")
        task = task_store.get_task(db, tid)

        refl = generate_reflection(db, task, [])
        deltas = refl.get("policy_deltas", [])
        assert len(deltas) > 0
        assert deltas[0]["suggestion"] == "increase_budget"

    def test_family_insights(self, db):
        from brain.reflection import generate_reflection, get_family_insights

        for i in range(3):
            tid = _create_completed_task(db, goal=f"Research task {i}")
            task = task_store.get_task(db, tid)
            ev = brain_evidence.get_evidence_for_task(tid, db)
            generate_reflection(db, task, ev)

        insights = get_family_insights(db, "research")
        assert insights["total"] == 3
        assert insights["success_count"] == 3
        assert "success" in insights["root_cause_distribution"]
        assert len(insights["common_patterns"]) > 0

    def test_get_reflections_for_family(self, db):
        from brain.reflection import generate_reflection, get_reflections_for_family

        tid = _create_completed_task(db)
        task = task_store.get_task(db, tid)
        ev = brain_evidence.get_evidence_for_task(tid, db)
        generate_reflection(db, task, ev)

        refls = get_reflections_for_family(db, "research")
        assert len(refls) == 1
        assert refls[0]["task_family"] == "research"


# ══════════════════════════════════════════════════════════════════
# Full Learning Pipeline Test
# ══════════════════════════════════════════════════════════════════

class TestLearningPipeline:
    def test_full_pipeline(self, db):
        """Simulate the complete post-task learning flow."""
        from brain.reflection import generate_reflection
        from brain.memory_curator import curate_after_task
        from brain.skill_engine import generate_candidate, auto_promote, search_skills
        from brain.memory import get_memory_stats

        # 1. Complete a task
        tid = _create_completed_task(db)
        task = task_store.get_task(db, tid)
        ev = brain_evidence.get_evidence_for_task(tid, db)
        plan = json.loads(task["plan_json"])
        verification = json.loads(task["verification_json"])

        # 2. Reflection
        refl = generate_reflection(db, task, ev, verification=verification, plan=plan)
        assert refl["root_cause_class"] == "success"

        # 3. Memory curation
        curation = curate_after_task(db, tid, task, ev, "pass", plan)
        assert curation["episodic_written"] is not None
        assert curation["skill_candidate"] is True

        # 4. Skill generation
        skill_id = generate_candidate(db, task, plan, ev)
        assert skill_id is not None
        auto_promote(db, skill_id)

        # 5. Verify learning artifacts exist
        stats = get_memory_stats(db, "sess_v2")
        assert stats["total"] >= 2  # at least episodic + semantic

        skills = search_skills(db, intent_family="research_general")
        assert len(skills) == 1

        # 6. Second task of same type — skill should be findable
        tid2 = _create_completed_task(db, goal="Research Python testing frameworks")
        skills_for_t2 = search_skills(db, intent_family="research_general")
        assert len(skills_for_t2) >= 1  # skill from t1 is available
