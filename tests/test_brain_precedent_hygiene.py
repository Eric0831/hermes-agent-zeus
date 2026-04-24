"""Tests for precedent hygiene filters."""

import json

from hermes_state import SessionDB
from brain import task_store, evidence as brain_evidence


def test_precedent_hygiene_rejects_media_and_low_evidence():
    from brain.precedent_hygiene import is_clean_task_precedent

    ok, reason = is_clean_task_precedent(
        family="coding",
        goal="修改為性感日式和服風 image_url: /tmp/a.jpg",
        evidence_count=40,
    )
    assert ok is False
    assert reason == "media_or_image_task"

    ok, reason = is_clean_task_precedent(
        family="general",
        goal="檢查系統運作",
        evidence_count=2,
    )
    assert ok is False
    assert reason.startswith("low_evidence")


def test_world_state_filters_noisy_precedents(tmp_path):
    from brain.world_state import get_world_state_summary

    db = SessionDB(tmp_path / "hygiene_world.db")
    db.create_session("sess_hygiene", source="test", user_id="u1")

    def _seed(conn):
        conn.execute(
            """INSERT INTO precedent_records
               (id, precedent_type, subject_type, subject_id, decision_json,
                binding_strength, source_review_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "prec_media",
                "family_pattern:coding",
                "task_family",
                "coding",
                json.dumps({
                    "family": "coding",
                    "goal": "修改為性感日式和服風 image_url: /tmp/a.jpg",
                    "verification": "pass",
                    "evidence_count": 50,
                }),
                0.95,
                None,
                2.0,
            ),
        )
        conn.execute(
            """INSERT INTO precedent_records
               (id, precedent_type, subject_type, subject_id, decision_json,
                binding_strength, source_review_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "prec_clean",
                "family_pattern:coding",
                "task_family",
                "coding",
                json.dumps({
                    "family": "coding",
                    "goal": "修復 embedding model 並測試服務",
                    "verification": "pass",
                    "evidence_count": 12,
                }),
                0.70,
                None,
                1.0,
            ),
        )

    db._execute_write(_seed)
    summary = get_world_state_summary(
        db,
        "sess_hygiene",
        goal="修復 embedding model",
        task_type="coding",
    )
    assert "修復 embedding model" in summary
    assert "性感日式和服" not in summary
    assert "image_url" not in summary


def test_extract_precedents_skips_noisy_tasks(tmp_path):
    from brain.capability_manager import create_version, execute_action

    db = SessionDB(tmp_path / "hygiene_extract.db")
    db.create_session("sess_hygiene", source="test", user_id="u1")

    noisy = task_store.create_task(
        db,
        "sess_hygiene",
        goal="修改為性感日式和服風 image_url: /tmp/a.jpg",
        task_type="coding",
    )
    clean = task_store.create_task(
        db,
        "sess_hygiene",
        goal="修復 embedding model 並測試服務",
        task_type="coding",
    )
    for tid in (noisy, clean):
        task_store.update_task_status(db, tid, "triaged")
        task_store.update_task_status(
            db,
            tid,
            "planned",
            plan_json=json.dumps({"goal": "test", "success_criteria": ["done"]}),
        )
        task_store.update_task_status(db, tid, "running")
        for i in range(6):
            brain_evidence.capture_from_tool_result(
                tid, "terminal", f"tc_{tid}_{i}", "ok", db,
            )
        task_store.update_task_status(db, tid, "verifying")
        task_store.update_task_status(db, tid, "completed", verification_status="pass")

    vid = create_version(
        db,
        "new_skill_family:coding",
        {
            "source": {
                "action_hint": {
                    "kind": "extract_precedents",
                    "task_family": "coding",
                    "limit": 5,
                }
            }
        },
    )
    result = execute_action(db, vid)

    assert result["executed"] is True
    payload = result["result"]
    assert payload["precedents_created"] == 1
    assert payload["tasks_skipped"] == 1

    rows = db._conn.execute(
        "SELECT decision_json FROM precedent_records WHERE subject_id='coding'"
    ).fetchall()
    text = "\n".join(r["decision_json"] for r in rows)
    assert "修復 embedding model" in text
    assert "性感日式和服" not in text
