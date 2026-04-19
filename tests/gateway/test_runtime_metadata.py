from pathlib import Path

import gateway.status as status
from gateway.runtime_metadata import collect_runtime_metadata


def test_collect_runtime_metadata(monkeypatch, tmp_path):
    project_root = tmp_path / "repo"
    project_root.mkdir()
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("model:\n  default: gpt-test\n", encoding="utf-8")

    monkeypatch.setattr("gateway.runtime_metadata.get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(
        "gateway.runtime_metadata.load_config",
        lambda: {"model": {"default": "gpt-test"}},
    )
    monkeypatch.setattr(
        "gateway.runtime_metadata.subprocess.run",
        lambda *args, **kwargs: type("R", (), {"stdout": "abc123def456\n"})(),
    )

    metadata = collect_runtime_metadata(project_root=project_root)
    assert metadata["git_sha"] == "abc123def456"
    assert metadata["model_name"] == "gpt-test"
    assert metadata["config_hash"] != "missing"
    assert metadata["prompt_version"].startswith("identity-")


def test_write_runtime_status_persists_runtime_metadata(monkeypatch, tmp_path):
    pid_path = tmp_path / "gateway.pid"
    status_path = tmp_path / "gateway_state.json"
    monkeypatch.setattr(status, "_get_pid_path", lambda: pid_path)
    monkeypatch.setattr(status, "_get_runtime_status_path", lambda: status_path)
    monkeypatch.setattr(status, "_get_process_start_time", lambda pid: 123)

    status.write_runtime_status(
        gateway_state="running",
        runtime_metadata={
            "git_sha": "deadbeef",
            "config_hash": "cafebabe",
            "prompt_version": "identity-1234",
            "model_name": "gpt-test",
        },
    )

    payload = status.read_runtime_status()
    assert payload["runtime_metadata"]["git_sha"] == "deadbeef"
    assert payload["runtime_metadata"]["model_name"] == "gpt-test"


def test_write_runtime_status_persists_last_agent_failure(monkeypatch, tmp_path):
    pid_path = tmp_path / "gateway.pid"
    status_path = tmp_path / "gateway_state.json"
    monkeypatch.setattr(status, "_get_pid_path", lambda: pid_path)
    monkeypatch.setattr(status, "_get_runtime_status_path", lambda: status_path)
    monkeypatch.setattr(status, "_get_process_start_time", lambda pid: 123)

    status.write_runtime_status(
        gateway_state="running",
        last_agent_failure={
            "error_code": "rate_limited",
            "error": "429 from provider",
            "session_id": "sess-1",
            "platform": "telegram",
            "failed_at": "2026-03-29T00:00:00Z",
        },
    )

    payload = status.read_runtime_status()
    assert payload["last_agent_failure"]["error_code"] == "rate_limited"
    assert payload["last_agent_failure"]["session_id"] == "sess-1"


def test_write_runtime_status_persists_last_monitor_check(monkeypatch, tmp_path):
    pid_path = tmp_path / "gateway.pid"
    status_path = tmp_path / "gateway_state.json"
    monkeypatch.setattr(status, "_get_pid_path", lambda: pid_path)
    monkeypatch.setattr(status, "_get_runtime_status_path", lambda: status_path)
    monkeypatch.setattr(status, "_get_process_start_time", lambda pid: 123)

    status.write_runtime_status(
        gateway_state="running",
        last_monitor_check={
            "checked_at": "2026-03-29T00:00:00Z",
            "health": "degraded",
            "action": "restart",
            "reason": "degraded:timeout",
            "restart_count_window": 1,
            "restart_budget_max": 3,
        },
    )

    payload = status.read_runtime_status()
    assert payload["last_monitor_check"]["action"] == "restart"
    assert payload["last_monitor_check"]["reason"] == "degraded:timeout"


def test_write_runtime_status_persists_last_preflight_check(monkeypatch, tmp_path):
    pid_path = tmp_path / "gateway.pid"
    status_path = tmp_path / "gateway_state.json"
    monkeypatch.setattr(status, "_get_pid_path", lambda: pid_path)
    monkeypatch.setattr(status, "_get_runtime_status_path", lambda: status_path)
    monkeypatch.setattr(status, "_get_process_start_time", lambda pid: 123)

    status.write_runtime_status(
        gateway_state="running",
        last_preflight_check={
            "checked_at": "2026-03-29T00:00:00Z",
            "status": "failed",
            "exit_code": 2,
            "issues": ["runtime_mismatch:git_sha"],
            "mismatches": {"git_sha": {"running": "old", "desired": "new"}},
        },
    )

    payload = status.read_runtime_status()
    assert payload["last_preflight_check"]["status"] == "failed"
    assert payload["last_preflight_check"]["exit_code"] == 2
