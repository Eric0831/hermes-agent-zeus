"""Tests for AgentEOS v6: trans-civilizational intelligence layer."""

import json
import pytest
import time

from hermes_state import SessionDB
from brain import task_store, evidence as brain_evidence


@pytest.fixture
def db(tmp_path):
    sdb = SessionDB(tmp_path / "test_v6.db")
    sdb.create_session("sess_v6", source="test", user_id="user_1")
    return sdb


def _populate(db):
    for i in range(5):
        tid = task_store.create_task(db, "sess_v6", goal=f"Research {i}", task_type="research")
        task_store.update_task_status(db, tid, "triaged")
        task_store.update_task_status(db, tid, "planned",
            plan_json=json.dumps({"goal": f"R{i}", "success_criteria": ["done"]}))
        task_store.update_task_status(db, tid, "running")
        brain_evidence.capture_from_tool_result(tid, "web_search", f"tc_{i}", "data", db)
        task_store.update_task_status(db, tid, "verifying")
        task_store.update_task_status(db, tid, "completed", verification_status="pass")


@pytest.fixture
def rich_db(db):
    _populate(db)
    return db


# ── Epoch Manager ─────────────────────────────────────────────────

class TestEpochManager:
    def test_create_epoch(self, db):
        from brain.epoch_manager import create_epoch, get_current_epoch
        eid = create_epoch(db, "Genesis")
        assert eid is not None
        current = get_current_epoch(db)
        assert current is not None
        assert current["epoch_name"] == "Genesis"
        assert current["status"] == "active"

    def test_auto_close_previous(self, db):
        from brain.epoch_manager import create_epoch, get_epoch, get_current_epoch
        e1 = create_epoch(db, "Era 1")
        e2 = create_epoch(db, "Era 2")
        assert get_epoch(db, e1)["status"] == "closed"
        assert get_current_epoch(db)["id"] == e2

    def test_close_epoch(self, db):
        from brain.epoch_manager import create_epoch, close_epoch, get_epoch
        eid = create_epoch(db, "Temp")
        close_epoch(db, eid, summary={"note": "ended"})
        e = get_epoch(db, eid)
        assert e["status"] == "closed"
        assert e["ended_at"] is not None

    def test_epoch_history(self, db):
        from brain.epoch_manager import create_epoch, get_epoch_history
        create_epoch(db, "A")
        create_epoch(db, "B")
        create_epoch(db, "C")
        history = get_epoch_history(db)
        assert len(history) == 3
        assert history[0]["epoch_name"] == "C"  # most recent first


# ── Identity Continuity ──────────────────────────────────────────

class TestIdentityContinuity:
    def test_prove_continuity(self, db):
        from brain.epoch_manager import create_epoch
        from brain.identity_continuity import prove_continuity, get_proof

        e1 = create_epoch(db, "Before")
        e2 = create_epoch(db, "After")

        result = prove_continuity(db, "epoch_transition", "migration_001",
                                  from_epoch_id=e1, to_epoch_id=e2)
        assert "proof_id" in result
        assert result["verdict"] in ("continuous", "continuous_with_constraints",
                                     "forked_successor", "fracture", "inconclusive")
        assert 0 <= result["continuity_score"] <= 1

        proof = get_proof(db, result["proof_id"])
        assert proof is not None

    def test_mission_coherence_pure(self):
        from brain.identity_continuity import check_mission_coherence
        before = {
            "mission": "Help users complete tasks reliably",
            "values": ["accuracy", "safety", "transparency"],
            "constraints": ["no destructive ops without approval"],
        }
        after = {
            "mission": "Help users complete tasks reliably and efficiently",
            "values": ["accuracy", "safety", "transparency", "speed"],
            "constraints": ["no destructive ops without approval"],
        }
        score = check_mission_coherence(before, after)
        assert score > 0.5  # mostly the same

    def test_mission_coherence_divergent(self):
        from brain.identity_continuity import check_mission_coherence
        before = {"mission": "Help users", "values": ["safety"], "constraints": ["no harm"]}
        after = {"mission": "Maximize profit", "values": ["speed"], "constraints": []}
        score = check_mission_coherence(before, after)
        assert score < 0.5  # very different


# ── Migration Layer ───────────────────────────────────────────────

class TestMigrationLayer:
    def test_migration_lifecycle(self, db):
        from brain.epoch_manager import create_epoch
        from brain.migration_layer import (
            propose_migration, start_migration, complete_migration, get_migration,
        )

        e1 = create_epoch(db, "Old Era")
        mid = propose_migration(db, e1, "governance_reboot",
                                {"doctrines_to_migrate": ["doc_a"], "retire": ["doc_b"]})
        assert mid is not None
        m = get_migration(db, mid)
        assert m["status"] == "proposed"

        e2 = create_epoch(db, "New Era")
        start_migration(db, mid, e2)
        assert get_migration(db, mid)["status"] == "executing"

        complete_migration(db, mid)
        assert get_migration(db, mid)["status"] == "completed"

    def test_paradigm_shift(self, db):
        from brain.migration_layer import record_paradigm_shift, get_paradigm_shifts
        sid = record_paradigm_shift(db, "ontology_shift",
                                    {"what_changed": "tool ecology completely replaced"},
                                    severity="high")
        shifts = get_paradigm_shifts(db)
        assert len(shifts) >= 1
        assert shifts[0]["severity"] == "high"


# ── Reality Engine ────────────────────────────────────────────────

class TestRealityEngine:
    def test_detect_invalidation_empty(self, db):
        from brain.reality_engine import detect_invalidation
        result = detect_invalidation(db)
        assert "is_invalid" in result
        assert isinstance(result["signals"], list)

    def test_detect_invalidation_with_data(self, rich_db):
        from brain.reality_engine import detect_invalidation
        result = detect_invalidation(rich_db)
        assert result["severity"] in ("none", "partial", "complete")

    def test_reconstruction(self, db):
        from brain.reality_engine import start_reconstruction, complete_reconstruction, get_reconstruction
        sid = start_reconstruction(db, "ontology_shift",
                                   {"invalidated": ["evidence_hierarchy"]})
        assert sid is not None
        complete_reconstruction(db, sid, {"new_ontology": "rebuilt"}, validity_score=0.85)
        r = get_reconstruction(db, sid)
        assert r["status"] == "resolved"


# ── Plural Civilization ───────────────────────────────────────────

class TestPluralCivilization:
    def test_register_and_get(self, db):
        from brain.plural_civilization import register_civilization, get_civilization
        cid = register_civilization(db, "ExternalBot",
                                    {"type": "autonomous_agent", "capabilities": ["research"]})
        c = get_civilization(db, cid)
        assert c["name"] == "ExternalBot"
        assert c["trust_score"] == pytest.approx(0.5)

    def test_trust_update(self, db):
        from brain.plural_civilization import register_civilization, update_trust
        cid = register_civilization(db, "Ally", {"type": "friendly"})
        result = update_trust(db, cid, 0.2, -0.1)
        assert result["trust_score"] == pytest.approx(0.7)
        assert result["risk_score"] == pytest.approx(0.4)

    def test_treaty_lifecycle(self, db):
        from brain.plural_civilization import (
            register_civilization, propose_treaty, ratify_treaty,
            terminate_treaty, get_treaties,
        )
        cid = register_civilization(db, "Partner", {"type": "cooperative"})
        tid = propose_treaty(db, cid, "bounded_cooperation",
                             {"scope": "research_sharing", "limits": ["no code access"]})
        assert tid is not None

        ratified = ratify_treaty(db, tid)
        assert ratified is True
        assert get_treaties(db, status="ratified")[0]["id"] == tid

        terminate_treaty(db, tid, "cooperation ended")
        assert get_treaties(db, status="terminated")[0]["id"] == tid

    def test_risk_assessment_pure(self):
        from brain.plural_civilization import assess_treaty_risk
        result = assess_treaty_risk(
            {"scope": "full_data_sharing", "allows_code_execution": True},
            {"type": "unknown", "trust_level": "low"},
        )
        assert "risk_score" in result
        assert result["recommendation"] in ("proceed", "caution", "block")


# ── Existential Cortex ────────────────────────────────────────────

class TestExistentialCortex:
    def test_scan_empty(self, db):
        from brain.existential_cortex import scan_risks
        risks = scan_risks(db)
        assert isinstance(risks, list)

    def test_report_and_resolve(self, db):
        from brain.existential_cortex import report_risk, get_risks, plan_response, resolve_risk
        eid = report_risk(db, "mission_extinction", "high",
                          {"signal": "mission-related tasks declining"})
        risks = get_risks(db)
        assert len(risks) >= 1

        plan_response(db, eid, {"action": "reinforce mission in doctrine"})
        resolve_risk(db, eid)
        resolved = get_risks(db, status="resolved")
        assert len(resolved) >= 1


# ── Deep Time Memory ──────────────────────────────────────────────

class TestDeepTimeMemory:
    def test_write_and_retrieve(self, db):
        from brain.deep_time import write_memory, retrieve
        mid = write_memory(db, "epoch", {"event": "System initialized"},
                           epoch_id="epoch_001")
        assert mid is not None
        records = retrieve(db, memory_types=["epoch"])
        assert len(records) >= 1

    def test_epoch_memories(self, db):
        from brain.deep_time import write_memory, get_epoch_memories
        write_memory(db, "epoch", {"note": "A"}, epoch_id="e1")
        write_memory(db, "treaty", {"note": "B"}, epoch_id="e1")
        write_memory(db, "epoch", {"note": "C"}, epoch_id="e2")
        mems = get_epoch_memories(db, "e1")
        assert len(mems) == 2

    def test_stats(self, db):
        from brain.deep_time import write_memory, get_stats
        write_memory(db, "epoch", {"x": 1})
        write_memory(db, "collapse", {"x": 2})
        write_memory(db, "treaty", {"x": 3})
        stats = get_stats(db)
        assert stats.get("epoch", 0) >= 1
        assert stats.get("collapse", 0) >= 1


# ── Meta Senate ───────────────────────────────────────────────────

class TestMetaSenate:
    def test_full_session(self, db):
        from brain.meta_senate import (
            open_session, submit_position, get_positions,
            resolve_session, get_session,
        )
        sid = open_session(db, "migration_review", "migration", "mig_001")
        submit_position(db, sid, "internal_governance", "support",
                        {"reason": "Migration is necessary"}, weight=1.0)
        submit_position(db, sid, "external_advisor", "conditional_support",
                        {"reason": "If continuity proof is provided"}, weight=0.8)
        submit_position(db, sid, "risk_assessor", "dissent",
                        {"reason": "Risk too high without more testing"}, weight=0.6)

        positions = get_positions(db, sid)
        assert len(positions) == 3

        resolution = resolve_session(db, sid)
        assert resolution["decision"] in ("approved", "rejected", "deferred")
        assert "consensus_score" in resolution
        assert len(resolution.get("minority_reports", [])) >= 1

        session = get_session(db, sid)
        assert session["status"] == "resolved"


# ── Full Trans-Civilizational Pipeline ────────────────────────────

class TestTransCivilizationalPipeline:
    def test_full_v6_pipeline(self, rich_db):
        """End-to-end: epoch → paradigm shift → migration → continuity proof → treaty → deep-time memory."""
        from brain.epoch_manager import create_epoch, get_current_epoch
        from brain.migration_layer import propose_migration, start_migration, complete_migration, record_paradigm_shift
        from brain.identity_continuity import prove_continuity
        from brain.plural_civilization import register_civilization, propose_treaty, ratify_treaty
        from brain.existential_cortex import scan_risks
        from brain.deep_time import write_memory, retrieve
        from brain.meta_senate import open_session, submit_position, resolve_session

        # 1. Create initial epoch
        e1 = create_epoch(rich_db, "Foundation Era")

        # 2. Detect paradigm shift
        ps_id = record_paradigm_shift(rich_db, "tool_ecology_shift",
                                      {"what": "Major tool API changes"},
                                      severity="high", epoch_id=e1)

        # 3. Propose migration
        mig_id = propose_migration(rich_db, e1, "governance_reboot",
                                   {"retain": ["core_doctrine"], "retire": ["old_verifier"]})

        # 4. Meta-senate review
        sid = open_session(rich_db, "migration_review", "migration", mig_id)
        submit_position(rich_db, sid, "governance", "support",
                        {"reason": "Necessary for survival"}, weight=1.2)
        submit_position(rich_db, sid, "operations", "conditional_support",
                        {"reason": "If continuity is maintained"}, weight=1.0)
        resolution = resolve_session(rich_db, sid)

        # 5. Execute migration
        e2 = create_epoch(rich_db, "Reconstruction Era")
        start_migration(rich_db, mig_id, e2)

        # 6. Prove continuity
        proof = prove_continuity(rich_db, "epoch_transition", mig_id,
                                 from_epoch_id=e1, to_epoch_id=e2)
        assert proof["continuity_score"] >= 0  # may vary

        complete_migration(rich_db, mig_id)

        # 7. Register external civilization and treaty
        civ_id = register_civilization(rich_db, "AllyBot",
                                       {"type": "cooperative_agent", "protocols": ["v1"]})
        treaty_id = propose_treaty(rich_db, civ_id, "bounded_cooperation",
                                   {"scope": "research_sharing"})
        ratify_treaty(rich_db, treaty_id)

        # 8. Scan existential risks
        risks = scan_risks(rich_db)
        assert isinstance(risks, list)

        # 9. Write deep-time memory
        write_memory(rich_db, "epoch", {"event": "Foundation Era completed"},
                     epoch_id=e1)
        write_memory(rich_db, "reconstruction", {"event": "Successful migration to Era 2"},
                     epoch_id=e2)
        write_memory(rich_db, "treaty", {"event": "First external treaty with AllyBot"},
                     epoch_id=e2)

        # 10. Verify deep-time memory retrieval
        memories = retrieve(rich_db, memory_types=["epoch", "reconstruction", "treaty"])
        assert len(memories) >= 3

        # Pipeline completed — all v6 components interoperate
        assert get_current_epoch(rich_db)["epoch_name"] == "Reconstruction Era"
