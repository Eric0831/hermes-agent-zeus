"""Tests for AgentEOS policy boundary status reporting."""

import json
import time

from hermes_state import SessionDB


def test_policy_status_reports_risky_tool_coverage(tmp_path):
    from brain import evidence, policy, task_store
    from brain.policy_status import collect_status, format_status

    db = SessionDB(tmp_path / "policy_status.db")
    db.create_session("sess_policy", "test")

    approval_task = task_store.create_task(
        db,
        "sess_policy",
        "Deploy production fix",
        task_type="coding",
        risk_level="high",
        requires_approval=True,
    )
    task_id = task_store.create_task(
        db,
        "sess_policy",
        "Patch config safely",
        task_type="coding",
        risk_level="medium",
    )

    evidence.capture_from_tool_result(task_id, "terminal", "tc_terminal", "echo ok", db)
    evidence.capture_from_tool_result(task_id, "patch", "tc_patch", "patched config", db)
    evidence.capture_from_tool_result(task_id, "mystery_writer", "tc_mystery", "unknown write", db)
    db.append_message("sess_policy", "tool", "sent", tool_name="send_message")

    decision = policy.evaluate(
        "shell_exec",
        "terminal",
        task_id=task_id,
        task_risk_level="medium",
        context={"command": "echo ok"},
        db=db,
    )
    assert decision.decision == "allow_with_approval"

    now = time.time()

    def _seed_version(conn):
        conn.execute(
            """INSERT INTO capability_proposals
               (id, proposal_type, target_task_family, title, proposal_json,
                expected_gain, risk_score, source_run_id, status,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "cprop_policy",
                "policy",
                "coding",
                "Risky coding tool rollout",
                json.dumps({"action_hint": {"kind": "update_recommended_tools"}}),
                0.25,
                0.45,
                "mlr_policy",
                "approved",
                now,
                now,
            ),
        )
        conn.execute(
            """INSERT INTO capability_versions
               (id, capability_family, version, status, definition_json,
                source_proposal_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "capv_policy",
                "policy:coding",
                1,
                "limited_rollout",
                json.dumps({"source": {"action_hint": {"kind": "update_recommended_tools"}}}),
                "cprop_policy",
                now,
            ),
        )

    db._execute_write(_seed_version)

    snapshot = collect_status(db)
    assert snapshot["policy_evaluations"]["decision_counts"]["allow_with_approval"] == 1
    assert snapshot["risky_tool_evidence"]["total"] == 2
    assert snapshot["risky_tool_evidence"]["covered"] == 1
    assert snapshot["risky_tool_evidence"]["uncovered"] == 1
    assert snapshot["risky_tool_evidence"]["coverage_pct"] == 50.0
    assert snapshot["open_approval_tasks"]["total"] == 1
    assert snapshot["open_approval_tasks"]["samples"][0]["id"] == approval_task
    assert snapshot["open_risky_versions"]["total"] == 1
    assert snapshot["unmapped_tools"]["total"] == 1

    boundary = {item["target"]: item for item in snapshot["boundary_targets"]}
    assert boundary["send_message"]["message_tool_count"] == 1
    assert boundary["terminal"]["policy_eval_count"] == 1
    assert boundary["patch"]["evidence_count"] == 1

    text = format_status(snapshot)
    assert "Policy Boundary Status" in text
    assert "coverage=50.0%" in text
    assert "allow_with_approval=1" in text
    assert "capv_policy" in text
    assert "mystery_writer" in text
    assert "Read-only report" in text


def test_policy_status_without_db():
    from brain.policy_status import collect_status, format_status

    snapshot = collect_status(None)
    assert snapshot["present"] is False
    assert "no_db" in format_status(snapshot)
