"""Tests for Evolution Dynamics v1.1: Gene-Culture, Epigenetic, Multilevel Selection, Criticality."""

import json
import pytest
import time

from hermes_state import SessionDB
from brain import task_store, evidence as brain_evidence


@pytest.fixture
def db(tmp_path):
    sdb = SessionDB(tmp_path / "test_ed11.db")
    sdb.create_session("sess_ed11", source="test", user_id="user_1")
    return sdb


def _populate_tasks(db, n=10):
    for i in range(n):
        tid = task_store.create_task(db, "sess_ed11", goal=f"Task {i}", task_type="research")
        task_store.update_task_status(db, tid, "triaged")
        task_store.update_task_status(db, tid, "planned",
            plan_json=json.dumps({"goal": f"T{i}", "success_criteria": ["done"]}))
        task_store.update_task_status(db, tid, "running")
        brain_evidence.capture_from_tool_result(tid, "web_search", f"tc_{i}", "data", db)
        task_store.update_task_status(db, tid, "verifying")
        task_store.update_task_status(db, tid, "completed", verification_status="pass")


@pytest.fixture
def rich_db(db):
    _populate_tasks(db)
    return db


# ── Gene-Culture ──────────────────────────────────────────────────

class TestGeneCulture:
    def test_classify_micro(self):
        from brain.gene_culture import classify_unit
        result = classify_unit({"layer": "micro", "unit_type": "skill"})
        assert result["inheritance_mode"] == "culture_like"
        assert result["transmission_mode"] == "horizontal_allowed"
        assert result["stability_class"] == "high_variation"

    def test_classify_macro(self):
        from brain.gene_culture import classify_unit
        result = classify_unit({"layer": "macro", "unit_type": "doctrine"})
        assert result["inheritance_mode"] == "gene_like"
        assert result["stability_class"] == "stable_core"

    def test_classify_meso(self):
        from brain.gene_culture import classify_unit
        result = classify_unit({"layer": "meso", "unit_type": "policy"})
        assert result["inheritance_mode"] == "hybrid"

    def test_gene_culture_fitness_stable(self):
        from brain.gene_culture import calculate_gene_culture_fitness
        result = calculate_gene_culture_fitness(0.8, "stable_core")
        assert result["rho"] == pytest.approx(0.8)
        assert result["f_gene"] == pytest.approx(0.8 * 0.8)
        assert result["f_culture"] == pytest.approx(0.2 * 0.8)
        assert result["f_total"] == pytest.approx(0.8)

    def test_gene_culture_fitness_variation(self):
        from brain.gene_culture import calculate_gene_culture_fitness
        result = calculate_gene_culture_fitness(0.6, "high_variation")
        assert result["rho"] == pytest.approx(0.2)

    def test_record_transmission(self, db):
        from brain.gene_culture import record_transmission, get_transmissions
        from brain.evolution_core import create_unit
        uid = create_unit(db, "skill", "micro", "research", {"x": 1},
                          transmission_mode="horizontal_allowed")
        tid = record_transmission(db, uid, "horizontal_culture",
                                  "family_a", "family_b", "allowed")
        assert tid is not None
        txs = get_transmissions(db, uid)
        assert len(txs) >= 1

    def test_horizontal_check(self, db):
        from brain.gene_culture import can_transmit_horizontally
        from brain.evolution_core import create_unit
        uid = create_unit(db, "skill", "micro", "research", {"x": 1},
                          transmission_mode="horizontal_allowed")
        assert can_transmit_horizontally(db, uid) is True

        uid2 = create_unit(db, "doctrine", "macro", "governance", {"x": 1},
                           transmission_mode="vertical_only")
        assert can_transmit_horizontally(db, uid2) is False


# ── Epigenetics ───────────────────────────────────────────────────

class TestEpigenetics:
    def test_create_and_get_marker(self, db):
        from brain.evolution_core import create_unit
        from brain.epigenetics import create_marker, get_markers
        uid = create_unit(db, "skill", "micro", "research", {"x": 1})
        mid = create_marker(db, uid, "high_risk_task", 1.3, "enhanced")
        assert mid is not None
        markers = get_markers(db, uid)
        assert len(markers) >= 1
        assert markers[0]["activation_state"] == "enhanced"

    def test_apply_expression_enhanced(self):
        from brain.epigenetics import apply_expression
        markers = [
            {"context_type": "high_risk", "activation_state": "enhanced",
             "expression_weight": 1.3},
        ]
        result = apply_expression(0.8, markers, "high_risk")
        assert result == pytest.approx(0.8 * 1.3)

    def test_apply_expression_suppressed(self):
        from brain.epigenetics import apply_expression
        markers = [
            {"context_type": "low_priority", "activation_state": "suppressed",
             "expression_weight": 0.4},
        ]
        result = apply_expression(0.8, markers, "low_priority")
        assert result == pytest.approx(0.8 * 0.4)

    def test_apply_expression_no_match(self):
        from brain.epigenetics import apply_expression
        markers = [
            {"context_type": "high_risk", "activation_state": "enhanced",
             "expression_weight": 1.3},
        ]
        result = apply_expression(0.8, markers, "other_context")
        assert result == pytest.approx(0.8)

    def test_expire_marker(self, db):
        from brain.evolution_core import create_unit
        from brain.epigenetics import create_marker, expire_marker, get_markers
        uid = create_unit(db, "skill", "micro", "r", {"x": 1})
        mid = create_marker(db, uid, "ctx", 1.2, "enhanced")
        expire_marker(db, mid)
        active = get_markers(db, uid)
        assert len(active) == 0  # expired markers filtered out

    def test_decay_markers(self, db):
        from brain.evolution_core import create_unit
        from brain.epigenetics import create_marker, decay_markers, get_markers
        uid = create_unit(db, "skill", "micro", "r", {"x": 1})
        create_marker(db, uid, "ctx", 1.3, "enhanced")
        adjusted = decay_markers(db, decay_rate=0.05)
        assert adjusted >= 1
        markers = get_markers(db, uid)
        if markers:
            assert markers[0]["expression_weight"] < 1.3

    def test_stats(self, db):
        from brain.evolution_core import create_unit
        from brain.epigenetics import create_marker, get_marker_stats
        uid = create_unit(db, "skill", "micro", "r", {"x": 1})
        create_marker(db, uid, "a", 1.3, "enhanced")
        create_marker(db, uid, "b", 0.4, "suppressed")
        stats = get_marker_stats(db)
        assert stats.get("enhanced", 0) >= 1
        assert stats.get("suppressed", 0) >= 1


# ── Multilevel Selection ─────────────────────────────────────────

class TestMultilevelSelection:
    def test_group_fitness(self, rich_db):
        from brain.multilevel_selection import calculate_group_fitness
        now = time.time()
        result = calculate_group_fitness(rich_db, "research", "family", "research",
                                         now - 86400, now)
        assert "run_id" in result
        assert "score" in result
        assert 0 <= result["score"] <= 1

    def test_multilevel_score(self, rich_db):
        from brain.evolution_core import create_unit, update_unit_status
        from brain.fitness_engine import calculate_micro_fitness
        from brain.multilevel_selection import calculate_group_fitness, calculate_multilevel_score

        now = time.time()
        uid = create_unit(rich_db, "skill", "micro", "research", {"x": 1})
        update_unit_status(rich_db, uid, "adopted")

        ind = calculate_micro_fitness(rich_db, uid, now - 86400, now)
        grp = calculate_group_fitness(rich_db, "research", "family", "research",
                                      now - 86400, now)

        result = calculate_multilevel_score(rich_db, uid, ind["fitness_run_id"],
                                           grp["run_id"], alpha=0.4)
        assert "total_score" in result
        expected = (1 - 0.4) * ind["score"] + 0.4 * grp["score"]
        assert result["total_score"] == pytest.approx(expected, abs=0.01)

    def test_recommended_alpha(self):
        from brain.multilevel_selection import get_recommended_alpha
        assert get_recommended_alpha("stable") == pytest.approx(0.35)
        assert get_recommended_alpha("elevated") == pytest.approx(0.60)
        assert get_recommended_alpha("critical") == pytest.approx(0.80)


# ── Criticality ───────────────────────────────────────────────────

class TestCriticality:
    def test_analyze_empty(self, db):
        from brain.criticality import analyze_criticality
        result = analyze_criticality(db, "civilization", "mainline")
        assert "snapshot_id" in result
        assert result["status"] in ("stable", "elevated", "critical")
        assert result["distance_to_critical"] >= 0

    def test_analyze_with_data(self, rich_db):
        from brain.criticality import analyze_criticality
        result = analyze_criticality(rich_db, "civilization", "mainline")
        assert result["status"] in ("stable", "elevated", "critical")

    def test_status_query(self, db):
        from brain.criticality import analyze_criticality, get_criticality_status
        analyze_criticality(db, "civ", "main")
        status = get_criticality_status(db, "civ", "main")
        assert status in ("stable", "elevated", "critical")

    def test_mutation_modifier(self):
        from brain.criticality import get_mutation_rate_modifier
        assert get_mutation_rate_modifier("stable") == 1.0
        assert get_mutation_rate_modifier("elevated") == 0.6
        assert get_mutation_rate_modifier("critical") == 0.2

    def test_rollout_modifier(self):
        from brain.criticality import get_rollout_modifier
        assert get_rollout_modifier("stable") == 1.0
        assert get_rollout_modifier("elevated") == 0.5
        assert get_rollout_modifier("critical") == 0.0

    def test_latest_snapshot(self, db):
        from brain.criticality import analyze_criticality, get_latest_snapshot
        analyze_criticality(db, "sys", "core")
        snap = get_latest_snapshot(db, "sys", "core")
        assert snap is not None
        assert snap["scope_type"] == "sys"


# ── Full ED v1.1 Pipeline ────────────────────────────────────────

class TestEDv11Pipeline:
    def test_full_pipeline(self, rich_db):
        """End-to-end: classify → epigenetic → multilevel → criticality → governed evolution."""
        from brain.evolution_core import create_unit, update_unit_status
        from brain.fitness_engine import calculate_micro_fitness
        from brain.gene_culture import classify_unit, calculate_gene_culture_fitness, can_transmit_horizontally
        from brain.epigenetics import create_marker, apply_expression
        from brain.multilevel_selection import calculate_group_fitness, calculate_multilevel_score, get_recommended_alpha
        from brain.criticality import analyze_criticality, get_mutation_rate_modifier
        from brain.selection_engine import evaluate_selection

        now = time.time()

        # 1. Create unit with gene-culture classification
        uid = create_unit(rich_db, "skill", "micro", "research", {"steps": ["search"]},
                          inheritance_mode="culture_like",
                          transmission_mode="horizontal_allowed",
                          stability_class="high_variation")
        update_unit_status(rich_db, uid, "adopted")

        # 2. Check classification
        unit = {"layer": "micro", "unit_type": "skill"}
        gc = classify_unit(unit)
        assert gc["inheritance_mode"] == "culture_like"

        # 3. Apply epigenetic marker for high-risk context
        create_marker(rich_db, uid, "high_risk_task", 1.3, "enhanced")

        # 4. Calculate individual fitness
        ind_fit = calculate_micro_fitness(rich_db, uid, now - 86400, now)

        # 5. Apply epigenetic expression
        markers = [{"context_type": "high_risk_task", "activation_state": "enhanced",
                     "expression_weight": 1.3}]
        effective_fitness = apply_expression(ind_fit["score"], markers, "high_risk_task")
        assert effective_fitness > ind_fit["score"]

        # 6. Gene-culture fitness split
        gc_fit = calculate_gene_culture_fitness(effective_fitness, "high_variation")
        assert gc_fit["rho"] == pytest.approx(0.2)

        # 7. Group fitness
        grp = calculate_group_fitness(rich_db, "research", "family", "research",
                                      now - 86400, now)

        # 8. Criticality check
        crit = analyze_criticality(rich_db, "civilization", "mainline")
        alpha = get_recommended_alpha(crit["status"])
        mut_mod = get_mutation_rate_modifier(crit["status"])

        # 9. Multilevel selection
        mls = calculate_multilevel_score(rich_db, uid, ind_fit["fitness_run_id"],
                                         grp["run_id"], alpha=alpha)

        # 10. Can transmit horizontally?
        can_h = can_transmit_horizontally(rich_db, uid)
        assert can_h is True  # culture_like, horizontal_allowed

        # Pipeline completed — all v1.1 layers interoperate
        assert mls["total_score"] >= 0
        assert mut_mod > 0
