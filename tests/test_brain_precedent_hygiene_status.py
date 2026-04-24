"""Tests for precedent hygiene status reporting."""

import json

from hermes_state import SessionDB


def test_precedent_hygiene_status_reports_rejected_samples(tmp_path):
    from brain.precedent_hygiene_status import collect_status, format_status

    db = SessionDB(tmp_path / "hygiene_status.db")

    def _seed(conn):
        conn.execute(
            """INSERT INTO precedent_records
               (id, precedent_type, subject_type, subject_id, decision_json,
                binding_strength, source_review_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "prec_bad_media",
                "family_pattern:coding",
                "task_family",
                "coding",
                json.dumps({
                    "family": "coding",
                    "goal": "修改圖片 image_url: /tmp/a.jpg",
                    "verification": "pass",
                    "evidence_count": 20,
                }),
                0.8,
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
                "prec_good",
                "family_pattern:coding",
                "task_family",
                "coding",
                json.dumps({
                    "family": "coding",
                    "goal": "修復 embedding model",
                    "verification": "pass",
                    "evidence_count": 10,
                }),
                0.7,
                None,
                1.0,
            ),
        )

    db._execute_write(_seed)
    snapshot = collect_status(db)

    assert snapshot["total_task_family_precedents"] == 2
    assert snapshot["clean"] == 1
    assert snapshot["rejected"] == 1
    assert snapshot["reason_counts"]["media_or_image_task"] == 1
    assert snapshot["samples"][0]["id"] == "prec_bad_media"

    text = format_status(snapshot)
    assert "Precedent Hygiene" in text
    assert "rejected=1" in text
    assert "media_or_image_task=1" in text
    assert "prec_bad_media" in text


def test_precedent_hygiene_status_no_rejections(tmp_path):
    from brain.precedent_hygiene_status import collect_status, format_status

    db = SessionDB(tmp_path / "hygiene_status_clean.db")
    snapshot = collect_status(db)

    assert snapshot["total_task_family_precedents"] == 0
    assert snapshot["samples"] == []
    assert "(none)" in format_status(snapshot)
