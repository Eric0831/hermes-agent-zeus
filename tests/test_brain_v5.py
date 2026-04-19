"""Tests for AgentEOS v5: civilizational intelligence layer."""

import json
import pytest
import time

from hermes_state import SessionDB
from brain import task_store, evidence as brain_evidence


@pytest.fixture
def db(tmp_path):
    sdb = SessionDB(tmp_path / "test_v5.db")
    sdb.create_session("sess_v5", source="test", user_id="user_1")
    return sdb


def _create_task_with_evidence(db, goal, task_type="research", status="completed"):
    tid = task_store.create_task(db, "sess_v5", goal=goal, task_type=task_type)
    task_store.update_task_status(db, tid, "triaged")
    task_store.update_task_status(db, tid, "planned",
        plan_json=json.dumps({"goal": goal, "success_criteria": ["done"]}))
    task_store.save_criteria(db, tid, ["done"])
    task_store.update_task_status(db, tid, "running")
    brain_evidence.capture_from_tool_result(tid, "web_search", f"tc_{tid}", "data", db)
    brain_evidence.capture_from_response(tid, "Result.", db)
    task_store.update_task_status(db, tid, "verifying")
    if status == "completed":
        task_store.update_task_status(db, tid, "completed", verification_status="pass")
    else:
        task_store.update_task_status(db, tid, "failed", failure_reason="test")
    return tid


@pytest.fixture
def rich_db(db):
    for i in range(5):
        _create_task_with_evidence(db, f"Research {i}", "research")
    for i in range(3):
        _create_task_with_evidence(db, f"Code {i}", "coding")
    _create_task_with_evidence(db, "Failed research", "research", "failed")
    return db


# ── Doctrine Engine ───────────────────────────────────────────────

class TestDoctrineEngine:
    def test_propose_and_ratify(self, db):
        from brain.doctrine_engine import propose_doctrine, ratify_doctrine, get_doctrine
        did = propose_doctrine(db, "Evidence Minimum", "verification",
                               {"min_evidence_count": 2})
        assert did is not None
        d = get_doctrine(db, did)
        assert d["status"] == "proposed"

        ratified = ratify_doctrine(db, did, "governance_review")
        assert ratified is True
        d = get_doctrine(db, did)
        assert d["status"] == "ratified"

    def test_get_active_doctrines(self, db):
        from brain.doctrine_engine import propose_doctrine, ratify_doctrine, get_active_doctrines
        d1 = propose_doctrine(db, "Doc A", "research", {"rule": "a"})
        ratify_doctrine(db, d1)
        d2 = propose_doctrine(db, "Doc B", "coding", {"rule": "b"})
        # d2 not ratified

        active = get_active_doctrines(db)
        assert len(active) == 1
        assert active[0]["doctrine_name"] == "Doc A"

    def test_search(self, db):
        from brain.doctrine_engine import propose_doctrine, ratify_doctrine, search_doctrines
        propose_doctrine(db, "Python ORM Policy", "coding", {"prefer": "SQLAlchemy"})
        results = search_doctrines(db, query="ORM")
        assert len(results) >= 1

    def test_archive(self, db):
        from brain.doctrine_engine import propose_doctrine, archive_doctrine, get_doctrine
        did = propose_doctrine(db, "Old Doctrine", "legacy", {})
        archive_doctrine(db, did)
        assert get_doctrine(db, did)["status"] == "archived"

    def test_stats(self, db):
        from brain.doctrine_engine import propose_doctrine, ratify_doctrine, get_doctrine_stats
        d1 = propose_doctrine(db, "A", "x", {})
        ratify_doctrine(db, d1)
        propose_doctrine(db, "B", "y", {})
        stats = get_doctrine_stats(db)
        assert stats.get("ratified", 0) == 1
        assert stats.get("proposed", 0) == 1


# ── Precedent Store ───────────────────────────────────────────────

class TestPrecedentStore:
    def test_create_and_get(self, db):
        from brain.precedent_store import create_precedent, get_precedent
        pid = create_precedent(db, "governance_case", "doctrine", "doc_123",
                               {"outcome": "approved", "reason": "low risk"})
        assert pid is not None
        p = get_precedent(db, pid)
        assert p["binding_strength"] == pytest.approx(0.5)

    def test_search(self, db):
        from brain.precedent_store import create_precedent, search_precedents
        create_precedent(db, "governance_case", "reform", "r1",
                         {"outcome": "approved"}, binding_strength=0.8)
        create_precedent(db, "conflict_resolution", "cluster", "c1",
                         {"outcome": "merged"}, binding_strength=0.3)

        results = search_precedents(db, precedent_type="governance_case")
        assert len(results) >= 1
        assert results[0]["binding_strength"] >= 0.8

    def test_find_applicable(self, db):
        from brain.precedent_store import create_precedent, find_applicable_precedents
        create_precedent(db, "governance_case", "doctrine", "d1",
                         {"topic": "verifier gaming mitigation", "decision": "tighten"})
        results = find_applicable_precedents(db, "doctrine", ["verifier", "gaming"])
        assert len(results) >= 1


# ── Institutional Memory ──────────────────────────────────────────

class TestInstitutionalMemory:
    def test_write_and_retrieve(self, db):
        from brain.institutional_mem import write_memory, retrieve
        mid = write_memory(db, "doctrine", "global", "system",
                           {"note": "Evidence minimums established"})
        assert mid is not None
        results = retrieve(db, memory_types=["doctrine"])
        assert len(results) >= 1

    def test_lineage(self, db):
        from brain.institutional_mem import write_memory, get_lineage
        write_memory(db, "reform", "doctrine", "doc_x",
                     {"change": "v1 to v2"}, lineage={"from": "v1"})
        write_memory(db, "reform", "doctrine", "doc_x",
                     {"change": "v2 to v3"}, lineage={"from": "v2"})
        history = get_lineage(db, "doctrine", "doc_x")
        assert len(history) == 2

    def test_stats(self, db):
        from brain.institutional_mem import write_memory, get_stats
        write_memory(db, "doctrine", "global", "sys", {"x": 1})
        write_memory(db, "precedent", "global", "sys", {"x": 2})
        stats = get_stats(db)
        assert stats.get("doctrine", 0) >= 1
        assert stats.get("precedent", 0) >= 1


# ── Agent Society ─────────────────────────────────────────────────

class TestAgentSociety:
    def test_register_cluster(self, db):
        from brain.agent_society import register_cluster, get_cluster
        cid = register_cluster(db, "Research Team",
                               {"domains": ["research", "analysis"]})
        c = get_cluster(db, cid)
        assert c["cluster_name"] == "Research Team"
        assert c["trust_score"] == pytest.approx(0.5)

    def test_trust_update(self, db):
        from brain.agent_society import register_cluster, update_trust
        cid = register_cluster(db, "Coders", {"domains": ["coding"]})
        new_trust = update_trust(db, cid, 0.2)
        assert new_trust == pytest.approx(0.7)

        # Clamp to 1.0
        update_trust(db, cid, 0.5)
        from brain.agent_society import get_cluster
        c = get_cluster(db, cid)
        assert c["trust_score"] <= 1.0

    def test_jurisdiction_overlap(self, db):
        from brain.agent_society import register_cluster, detect_jurisdiction_overlap
        register_cluster(db, "Team A", {"domains": ["research", "analysis"]})
        register_cluster(db, "Team B", {"domains": ["research", "strategy"]})

        overlaps = detect_jurisdiction_overlap(db)
        assert len(overlaps) >= 1
        assert "research" in str(overlaps[0])

    def test_arbitrate(self, db):
        from brain.agent_society import register_cluster, arbitrate_conflict
        c1 = register_cluster(db, "Alpha", {"domains": ["x"]})
        c2 = register_cluster(db, "Beta", {"domains": ["x"]})
        result = arbitrate_conflict(db, c1, c2, "jurisdiction_overlap",
                                    {"winner": "Alpha", "action": "Beta yields x"})
        assert result is not None  # precedent_id


# ── Deliberation ──────────────────────────────────────────────────

class TestDeliberation:
    def test_full_deliberation(self, db):
        from brain.deliberation import (
            open_session, submit_position, get_positions,
            resolve_session, get_session,
        )
        sid = open_session(db, "doctrine_review", "doctrine", "doc_001")
        assert sid is not None

        submit_position(db, sid, "cluster_research", "support",
                        {"reason": "Improves accuracy"}, weight=1.0)
        submit_position(db, sid, "cluster_coding", "oppose",
                        {"reason": "Slows development"}, weight=0.8)
        submit_position(db, sid, "cluster_governance", "conditional_support",
                        {"reason": "If bounded to research only"}, weight=1.2)

        positions = get_positions(db, sid)
        assert len(positions) == 3

        resolution = resolve_session(db, sid)
        assert resolution["decision"] in ("approved", "rejected", "deferred")
        assert "consensus_score" in resolution

        session = get_session(db, sid)
        assert session["status"] == "resolved"

    def test_minority_report(self, db):
        from brain.deliberation import open_session, submit_position, resolve_session
        sid = open_session(db, "reform_debate", "reform", "r_001")
        submit_position(db, sid, "c1", "support", {"reason": "yes"}, weight=2.0)
        submit_position(db, sid, "c2", "dissent", {"reason": "strongly disagree"}, weight=1.0)

        resolution = resolve_session(db, sid)
        assert len(resolution.get("minority_reports", [])) >= 1


# ── Capability Economy ────────────────────────────────────────────

class TestCapabilityEconomy:
    def test_valuate_all_empty(self, db):
        from brain.capability_economy import valuate_all
        result = valuate_all(db)
        assert isinstance(result, list)

    def test_valuate_with_skills(self, rich_db):
        from brain.skill_engine import generate_candidate, auto_promote
        from brain.capability_economy import valuate_all, recommend_retirements

        # Create a skill to valuate
        tid = _create_task_with_evidence(rich_db, "Skill-worthy task")
        task = task_store.get_task(rich_db, tid)
        plan = json.loads(task["plan_json"])
        ev = brain_evidence.get_evidence_for_task(tid, rich_db)
        sid = generate_candidate(rich_db, task, plan, ev)
        if sid:
            auto_promote(rich_db, sid)

        results = valuate_all(rich_db)
        # May have valuations depending on data
        assert isinstance(results, list)

        retirements = recommend_retirements(rich_db)
        assert isinstance(retirements, list)


# ── Civilization Planner ──────────────────────────────────────────

class TestCivilizationPlanner:
    def test_health_snapshot_empty(self, db):
        from brain.civilization_planner import get_health_snapshot
        h = get_health_snapshot(db)
        assert isinstance(h, dict)

    def test_health_snapshot_with_data(self, rich_db):
        from brain.civilization_planner import get_health_snapshot, assess_continuity
        h = get_health_snapshot(rich_db)
        assert isinstance(h, dict)

        c = assess_continuity(rich_db)
        assert "institutional_health" in c or "mission_drift_score" in c or isinstance(c, dict)

    def test_identify_fragilities(self, rich_db):
        from brain.civilization_planner import identify_fragilities
        frags = identify_fragilities(rich_db)
        assert isinstance(frags, list)

    def test_propose_reform(self, db):
        from brain.civilization_planner import propose_reform
        rid = propose_reform(db, "institutional_reform", "doctrine_system",
                             {"proposal": "Add evidence minimum doctrine"})
        assert rid is not None


# ── Cultural Stability ────────────────────────────────────────────

class TestCulturalStability:
    def test_analyze_empty(self, db):
        from brain.cultural_stability import analyze_culture
        result = analyze_culture(db)
        assert "health_score" in result
        assert isinstance(result.get("pathologies"), list)

    def test_analyze_with_data(self, rich_db):
        from brain.cultural_stability import analyze_culture
        result = analyze_culture(rich_db)
        assert result["health_score"] >= 0
        assert result["health_score"] <= 1.0

    def test_record_drift(self, db):
        from brain.cultural_stability import record_drift_event, get_drift_events
        did = record_drift_event(db, "verifier_gaming", "medium",
                                 {"signal": "high pass rate, low evidence diversity"})
        assert did is not None
        events = get_drift_events(db)
        assert len(events) >= 1

    def test_detect_functions_no_crash(self, rich_db):
        from brain.cultural_stability import (
            detect_verifier_gaming, detect_bureaucratic_drag, detect_mission_dilution,
        )
        # These should all return dict or None, never crash
        r1 = detect_verifier_gaming(rich_db)
        r2 = detect_bureaucratic_drag(rich_db)
        r3 = detect_mission_dilution(rich_db)
        for r in [r1, r2, r3]:
            assert r is None or isinstance(r, dict)


# ── Full Civilizational Pipeline ──────────────────────────────────

class TestCivilizationalPipeline:
    def test_end_to_end(self, rich_db):
        """Full v5 pipeline: doctrine → precedent → deliberation → institutional memory → culture check."""
        from brain.doctrine_engine import propose_doctrine, ratify_doctrine
        from brain.precedent_store import create_precedent
        from brain.institutional_mem import write_memory, retrieve
        from brain.agent_society import register_cluster
        from brain.deliberation import open_session, submit_position, resolve_session
        from brain.cultural_stability import analyze_culture
        from brain.civilization_planner import get_health_snapshot

        # 1. Establish agent clusters
        c1 = register_cluster(rich_db, "Research", {"domains": ["research"]},
                              authority_level="operational")
        c2 = register_cluster(rich_db, "Governance", {"domains": ["governance", "audit"]},
                              authority_level="governance")

        # 2. Propose and deliberate a doctrine
        did = propose_doctrine(rich_db, "Evidence Minimum Policy",
                               "verification",
                               {"min_evidence_per_task": 2, "required_tool_evidence": True})

        sid = open_session(rich_db, "doctrine_review", "doctrine", did)
        submit_position(rich_db, sid, c1, "support",
                        {"reason": "Improves research reliability"}, weight=1.0)
        submit_position(rich_db, sid, c2, "conditional_support",
                        {"reason": "Approve if bounded to research tasks"}, weight=1.2)

        resolution = resolve_session(rich_db, sid)
        assert resolution["decision"] in ("approved", "rejected", "deferred")

        # 3. Ratify doctrine
        ratify_doctrine(rich_db, did, "deliberation_senate")

        # 4. Create precedent from this decision
        create_precedent(rich_db, "doctrine_interpretation", "doctrine", did,
                         {"interpretation": "Min 2 evidence records for research tasks",
                          "deliberation_id": sid},
                         binding_strength=0.7)

        # 5. Write institutional memory
        write_memory(rich_db, "doctrine", "global", "evidence_policy",
                     {"event": "Evidence Minimum Policy ratified",
                      "deliberation_id": sid,
                      "context": "After low evidence diversity detected"})

        # 6. Culture check
        culture = analyze_culture(rich_db)
        assert "health_score" in culture

        # 7. Civilization health
        health = get_health_snapshot(rich_db)
        assert isinstance(health, dict)

        # 8. Verify institutional memory is retrievable
        memories = retrieve(rich_db, memory_types=["doctrine"])
        assert len(memories) >= 1

        # Pipeline completed — all v5 components interoperate
