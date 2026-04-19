"""Tests for AgentEOS Evolution Dynamics: core, fitness, selection, replicator, experiments."""

import json
import pytest
import time

from hermes_state import SessionDB
from brain import task_store, evidence as brain_evidence


@pytest.fixture
def db(tmp_path):
    sdb = SessionDB(tmp_path / "test_evo.db")
    sdb.create_session("sess_evo", source="test", user_id="user_1")
    return sdb


def _create_tasks(db, n=5, task_type="research"):
    """Create n completed tasks with evidence for fitness calculation."""
    for i in range(n):
        tid = task_store.create_task(db, "sess_evo", goal=f"Task {i}", task_type=task_type)
        task_store.update_task_status(db, tid, "triaged")
        task_store.update_task_status(db, tid, "planned",
            plan_json=json.dumps({"goal": f"T{i}", "success_criteria": ["done"]}))
        task_store.update_task_status(db, tid, "running")
        brain_evidence.capture_from_tool_result(tid, "web_search", f"tc_{i}", "data", db)
        task_store.update_task_status(db, tid, "verifying")
        task_store.update_task_status(db, tid, "completed", verification_status="pass")


@pytest.fixture
def rich_db(db):
    _create_tasks(db, 10)
    return db


# ── Evolution Core ────────────────────────────────────────────────

class TestEvolutionCore:
    def test_create_unit(self, db):
        from brain.evolution_core import create_unit, get_unit
        uid = create_unit(db, "skill", "micro", "research",
                          {"steps": ["search", "analyze"]})
        assert uid is not None
        u = get_unit(db, uid)
        assert u["status"] == "candidate"
        assert u["layer"] == "micro"
        assert u["family"] == "research"

    def test_mutate_unit(self, db):
        from brain.evolution_core import create_unit, mutate_unit, get_unit
        uid = create_unit(db, "skill", "micro", "research", {"v": 1})
        mid = mutate_unit(db, uid, "tool_substitution",
                          {"from": "search_v1", "to": "search_v2"})
        assert mid is not None
        u = get_unit(db, uid)
        assert u["status"] == "mutated"

    def test_get_units_filtered(self, db):
        from brain.evolution_core import create_unit, get_units
        create_unit(db, "skill", "micro", "research", {"x": 1})
        create_unit(db, "policy", "meso", "research", {"x": 2})
        create_unit(db, "skill", "micro", "coding", {"x": 3})

        micro = get_units(db, layer="micro")
        assert len(micro) == 2
        research = get_units(db, family="research")
        assert len(research) == 2

    def test_inheritance(self, db):
        from brain.evolution_core import create_unit, link_inheritance, get_lineage
        u1 = create_unit(db, "skill", "micro", "research", {"v": 1})
        u2 = create_unit(db, "skill", "micro", "research", {"v": 2},
                         parent_unit_id=u1)
        link_inheritance(db, u1, u2, "skill_promotion")
        lineage = get_lineage(db, u2)
        assert len(lineage) >= 1
        assert lineage[0]["parent_unit_id"] == u1

    def test_stats(self, db):
        from brain.evolution_core import create_unit, update_unit_status, get_unit_stats
        u1 = create_unit(db, "skill", "micro", "r", {"x": 1})
        u2 = create_unit(db, "policy", "meso", "r", {"x": 2})
        update_unit_status(db, u1, "adopted")
        stats = get_unit_stats(db)
        assert stats.get("total", 0) == 2


# ── Fitness Engine ────────────────────────────────────────────────

class TestFitnessEngine:
    def test_micro_fitness(self, rich_db):
        from brain.evolution_core import create_unit
        from brain.fitness_engine import calculate_micro_fitness, get_latest_fitness

        uid = create_unit(rich_db, "skill", "micro", "research", {"x": 1})
        now = time.time()
        result = calculate_micro_fitness(rich_db, uid, now - 86400, now)

        assert "fitness_run_id" in result
        assert "score" in result
        assert 0 <= result["score"] <= 1

        latest = get_latest_fitness(rich_db, uid)
        assert latest is not None
        assert latest["score"] == result["score"]

    def test_meso_fitness(self, rich_db):
        from brain.evolution_core import create_unit
        from brain.fitness_engine import calculate_meso_fitness

        uid = create_unit(rich_db, "policy", "meso", "research", {"x": 1})
        now = time.time()
        result = calculate_meso_fitness(rich_db, uid, now - 86400, now)
        assert "score" in result

    def test_macro_fitness(self, rich_db):
        from brain.evolution_core import create_unit
        from brain.fitness_engine import calculate_macro_fitness

        uid = create_unit(rich_db, "doctrine", "macro", "research", {"x": 1})
        now = time.time()
        result = calculate_macro_fitness(rich_db, uid, now - 86400, now)
        assert "score" in result

    def test_fitness_history(self, rich_db):
        from brain.evolution_core import create_unit
        from brain.fitness_engine import calculate_micro_fitness, get_fitness_history

        uid = create_unit(rich_db, "skill", "micro", "research", {"x": 1})
        now = time.time()
        calculate_micro_fitness(rich_db, uid, now - 86400, now)
        calculate_micro_fitness(rich_db, uid, now - 86400 * 2, now - 86400)

        history = get_fitness_history(rich_db, uid)
        assert len(history) == 2


# ── Selection Engine ──────────────────────────────────────────────

class TestSelectionEngine:
    def test_adopt_decision(self, rich_db):
        from brain.evolution_core import create_unit
        from brain.fitness_engine import calculate_micro_fitness
        from brain.selection_engine import evaluate_selection, get_decision

        uid = create_unit(rich_db, "skill", "micro", "research", {"x": 1})
        now = time.time()
        fit = calculate_micro_fitness(rich_db, uid, now - 86400, now)

        result = evaluate_selection(rich_db, uid, fit["fitness_run_id"],
                                    baseline_score=fit["score"] - 0.1)
        assert result["decision"] in ("adopt", "trial", "reject")
        assert result["decision_id"] is not None

        d = get_decision(rich_db, result["decision_id"])
        assert d is not None

    def test_reject_on_governance_fail(self, rich_db):
        from brain.evolution_core import create_unit
        from brain.fitness_engine import calculate_micro_fitness
        from brain.selection_engine import evaluate_selection

        uid = create_unit(rich_db, "skill", "micro", "research", {"x": 1})
        now = time.time()
        fit = calculate_micro_fitness(rich_db, uid, now - 86400, now)

        result = evaluate_selection(rich_db, uid, fit["fitness_run_id"],
                                    governance_pass=False, governance_conditional=False)
        assert result["decision"] == "reject"

    def test_trial_on_conditional(self, rich_db):
        from brain.evolution_core import create_unit
        from brain.fitness_engine import calculate_micro_fitness
        from brain.selection_engine import evaluate_selection

        uid = create_unit(rich_db, "skill", "micro", "research", {"x": 1})
        now = time.time()
        fit = calculate_micro_fitness(rich_db, uid, now - 86400, now)

        result = evaluate_selection(rich_db, uid, fit["fitness_run_id"],
                                    governance_pass=False, governance_conditional=True,
                                    baseline_score=fit["score"] - 0.03)
        assert result["decision"] == "trial"

    def test_adopt_reject_retire(self, db):
        from brain.evolution_core import create_unit, get_unit
        from brain.selection_engine import adopt_unit, reject_unit, retire_unit

        u1 = create_unit(db, "skill", "micro", "r", {"x": 1})
        adopt_unit(db, u1)
        assert get_unit(db, u1)["status"] == "adopted"

        u2 = create_unit(db, "skill", "micro", "r", {"x": 2})
        reject_unit(db, u2, "bad performance")
        assert get_unit(db, u2)["status"] == "rejected"

        u3 = create_unit(db, "skill", "micro", "r", {"x": 3})
        retire_unit(db, u3)
        assert get_unit(db, u3)["status"] == "retired"


# ── Replicator ────────────────────────────────────────────────────

class TestReplicator:
    def test_update_weights(self, rich_db):
        from brain.evolution_core import create_unit, update_unit_status
        from brain.fitness_engine import calculate_micro_fitness
        from brain.replicator import update_weights, get_weights

        now = time.time()
        u1 = create_unit(rich_db, "skill", "micro", "research", {"v": 1})
        u2 = create_unit(rich_db, "skill", "micro", "research", {"v": 2})
        update_unit_status(rich_db, u1, "adopted")
        update_unit_status(rich_db, u2, "adopted")

        calculate_micro_fitness(rich_db, u1, now - 86400, now)
        calculate_micro_fitness(rich_db, u2, now - 86400, now)

        weights = update_weights(rich_db, "research", "2026-04")
        assert len(weights) >= 2
        total = sum(w["weight"] for w in weights)
        assert abs(total - 1.0) < 0.01  # normalized

    def test_get_weights(self, rich_db):
        from brain.evolution_core import create_unit, update_unit_status
        from brain.fitness_engine import calculate_micro_fitness
        from brain.replicator import update_weights, get_weights

        now = time.time()
        u1 = create_unit(rich_db, "skill", "micro", "coding", {"v": 1})
        update_unit_status(rich_db, u1, "adopted")
        calculate_micro_fitness(rich_db, u1, now - 86400, now)
        update_weights(rich_db, "coding", "2026-04")

        w = get_weights(rich_db, "coding")
        assert len(w) >= 1

    def test_select_by_weight(self, rich_db):
        from brain.evolution_core import create_unit, update_unit_status
        from brain.fitness_engine import calculate_micro_fitness
        from brain.replicator import update_weights, select_by_weight

        now = time.time()
        u1 = create_unit(rich_db, "skill", "micro", "summary", {"v": 1})
        update_unit_status(rich_db, u1, "adopted")
        calculate_micro_fitness(rich_db, u1, now - 86400, now)
        update_weights(rich_db, "summary", "2026-04")

        selected = select_by_weight(rich_db, "summary")
        assert selected == u1  # only one unit


# ── Experiment Engine ─────────────────────────────────────────────

class TestExperimentEngine:
    def test_experiment_lifecycle(self, db):
        from brain.evolution_core import create_unit
        from brain.experiment_engine import (
            create_experiment, start_experiment, complete_experiment,
            get_experiment,
        )

        uid = create_unit(db, "skill", "micro", "research", {"x": 1})
        eid = create_experiment(db, "canary", uid, {"task_family": "research", "rollout": 10})
        assert eid is not None

        start_experiment(db, eid)
        assert get_experiment(db, eid)["status"] == "running"

        complete_experiment(db, eid, "won", metrics={"quality_delta": 0.08})
        assert get_experiment(db, eid)["status"] == "won"

    def test_rollback(self, db):
        from brain.evolution_core import create_unit, get_unit
        from brain.experiment_engine import create_experiment, start_experiment, rollback_experiment, get_experiment

        uid = create_unit(db, "policy", "meso", "research", {"x": 1})
        eid = create_experiment(db, "sandbox_trial", uid, {"scope": "test"})
        start_experiment(db, eid)
        rollback_experiment(db, eid, "regression detected")

        exp = get_experiment(db, eid)
        assert exp["status"] == "rolled_back"

    def test_get_experiments_filtered(self, db):
        from brain.evolution_core import create_unit
        from brain.experiment_engine import create_experiment, get_experiments

        u1 = create_unit(db, "skill", "micro", "research", {"x": 1})
        u2 = create_unit(db, "skill", "micro", "coding", {"x": 2})
        create_experiment(db, "canary", u1, {"scope": "r"})
        create_experiment(db, "shadow", u2, {"scope": "c"})

        all_exp = get_experiments(db)
        assert len(all_exp) == 2

        u1_exp = get_experiments(db, unit_id=u1)
        assert len(u1_exp) == 1


# ── Full Evolution Pipeline ──────────────────────────────────────

class TestEvolutionPipeline:
    def test_full_evolution_cycle(self, rich_db):
        """End-to-end: create → mutate → fitness → govern → select → experiment → adopt → replicate."""
        from brain.evolution_core import create_unit, mutate_unit, update_unit_status, link_inheritance
        from brain.fitness_engine import calculate_micro_fitness
        from brain.selection_engine import evaluate_selection, adopt_unit
        from brain.experiment_engine import create_experiment, start_experiment, complete_experiment
        from brain.replicator import update_weights, get_weights

        now = time.time()

        # 1. Create baseline unit
        baseline = create_unit(rich_db, "skill", "micro", "research",
                               {"steps": ["search", "analyze"], "version": "1.0"})
        update_unit_status(rich_db, baseline, "adopted")
        baseline_fit = calculate_micro_fitness(rich_db, baseline, now - 86400, now)

        # 2. Create candidate via mutation
        candidate = create_unit(rich_db, "skill", "micro", "research",
                                {"steps": ["search_v2", "analyze", "summarize"], "version": "1.1"},
                                parent_unit_id=baseline)
        mutate_unit(rich_db, candidate, "step_insert",
                    {"added": "summarize", "position": 2})

        # 3. Compute fitness for candidate
        candidate_fit = calculate_micro_fitness(rich_db, candidate, now - 86400, now)

        # 4. Selection (with governance=pass, use baseline as reference)
        sel = evaluate_selection(rich_db, candidate, candidate_fit["fitness_run_id"],
                                 baseline_score=baseline_fit["score"] - 0.06)

        # 5. If trial or adopt, run experiment
        if sel["decision"] in ("adopt", "trial"):
            eid = create_experiment(rich_db, "canary", candidate,
                                    {"task_family": "research", "rollout": 10},
                                    baseline_unit_id=baseline)
            start_experiment(rich_db, eid)
            complete_experiment(rich_db, eid, "won", {"quality_delta": 0.06})
            adopt_unit(rich_db, candidate)
        else:
            # Even if rejected, pipeline should complete without error
            pass

        # 6. Link inheritance
        link_inheritance(rich_db, baseline, candidate, "skill_promotion")

        # 7. Update replicator weights
        weights = update_weights(rich_db, "research", "2026-04")
        assert len(weights) >= 1

        # Pipeline completed
        from brain.evolution_core import get_unit
        c = get_unit(rich_db, candidate)
        assert c["status"] in ("adopted", "rejected", "trial")
