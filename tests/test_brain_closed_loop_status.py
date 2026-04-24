"""Tests for AgentEOS closed-loop status reporting."""

import json
import time

from hermes_state import SessionDB


def test_collect_and_format_closed_loop_status(tmp_path):
    from brain.closed_loop_status import collect_status, format_status
    from brain.capability_manager import create_version, transition_status

    db = SessionDB(tmp_path / "loop_status.db")
    now = time.time()

    def _seed(conn):
        conn.execute(
            """INSERT INTO capability_proposals
               (id, proposal_type, target_task_family, title, proposal_json,
                expected_gain, risk_score, source_run_id, status,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "cprop_loop",
                "new_skill_family",
                "general",
                "Extract general fast path",
                json.dumps({
                    "action_hint": {
                        "kind": "extract_skill",
                        "task_family": "general",
                    }
                }),
                0.3,
                0.1,
                "mlr_loop",
                "incubating",
                now,
                now,
            ),
        )
        conn.execute(
            """INSERT INTO skill_registry
               (id, skill_name, intent_family, version, status,
                definition_json, success_rate, usage_count, last_used_at,
                created_at, updated_at, risk_level, source_task_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "skill_loop_medium",
                "general fast path",
                "general_auto",
                "1.0",
                "candidate",
                "{}",
                1.0,
                0,
                None,
                now,
                now,
                "medium",
                None,
            ),
        )
        conn.execute(
            """CREATE TABLE closed_loop_runs (
               id TEXT PRIMARY KEY,
               gateway TEXT NOT NULL,
               version_id TEXT NOT NULL,
               source_proposal_id TEXT,
               action_kind TEXT,
               mode TEXT NOT NULL,
               before_status TEXT,
               after_status TEXT,
               decision TEXT NOT NULL,
               reason TEXT,
               result_json TEXT,
               metrics_before_json TEXT,
               metrics_after_json TEXT,
               created_at REAL NOT NULL
            )"""
        )

    db._execute_write(_seed)
    vid = create_version(
        db,
        "new_skill_family:general",
        {
            "source": {
                "action_hint": {
                    "kind": "extract_skill",
                    "task_family": "general",
                }
            }
        },
        source_proposal_id="cprop_loop",
    )
    transition_status(db, vid, "incubating")
    transition_status(db, vid, "experimental")
    transition_status(db, vid, "limited_rollout")

    def _seed_run(conn):
        conn.execute(
            """INSERT INTO closed_loop_runs
               (id, gateway, version_id, source_proposal_id, action_kind, mode,
                before_status, after_status, decision, reason, result_json,
                metrics_before_json, metrics_after_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "clr_loop",
                "main",
                vid,
                "cprop_loop",
                "extract_skill",
                "apply",
                "incubating",
                "limited_rollout",
                "limited_rollout",
                "action_executed",
                "{}",
                "{}",
                "{}",
                now,
            ),
        )

    db._execute_write(_seed_run)

    snapshot = collect_status(db)
    assert snapshot["capability_counts"]["limited_rollout"] == 1
    assert snapshot["skill_candidates"]["medium"] == 1
    assert snapshot["open_versions"][0]["action_kind"] == "extract_skill"
    assert snapshot["recent_runs"][0]["decision"] == "limited_rollout"

    text = format_status(snapshot)
    assert "Closed Loop Status" in text
    assert "limited_rollout=1" in text
    assert "medium=1" in text
    assert "extract_skill" in text


def test_closed_loop_status_without_runs_table(tmp_path):
    from brain.closed_loop_status import collect_status, format_status

    db = SessionDB(tmp_path / "loop_status_empty.db")
    snapshot = collect_status(db)
    assert snapshot["recent_runs"] == []
    assert "no closed_loop_runs logged" in format_status(snapshot)
