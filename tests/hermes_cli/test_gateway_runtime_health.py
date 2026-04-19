from hermes_cli.gateway import _runtime_health_lines


def test_runtime_health_lines_include_fatal_platform_and_startup_reason(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "startup_failed",
            "exit_reason": "telegram conflict",
            "platforms": {
                "telegram": {
                    "state": "fatal",
                    "error_message": "another poller is active",
                }
            },
        },
    )

    lines = _runtime_health_lines()

    assert "⚠ telegram: another poller is active" in lines
    assert "⚠ Last startup issue: telegram conflict" in lines


def test_runtime_health_lines_include_last_agent_failure(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "platforms": {},
            "last_agent_failure": {
                "error_code": "rate_limited",
                "error": "429 from provider",
                "session_id": "sess-1",
                "platform": "telegram",
            },
        },
    )

    lines = _runtime_health_lines()

    assert "⚠ Last agent failure: rate_limited (telegram / sess-1)" in lines
    assert "  429 from provider" in lines


def test_runtime_health_lines_include_last_monitor_check(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "platforms": {},
            "last_monitor_check": {
                "action": "restart",
                "reason": "degraded:timeout",
                "health": "degraded",
                "restart_count_window": 1,
                "restart_budget_max": 3,
            },
        },
    )

    lines = _runtime_health_lines()

    assert "• monitor: restart reason=degraded:timeout health=degraded restarts=1/3" in lines


def test_runtime_health_lines_include_last_preflight_check(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "platforms": {},
            "last_preflight_check": {
                "status": "failed",
                "exit_code": 2,
                "issues": ["runtime_mismatch:git_sha"],
                "mismatches": {"git_sha": {"running": "old", "desired": "new"}},
            },
        },
    )

    lines = _runtime_health_lines()

    assert "• preflight: failed exit=2 issues=1 mismatches=1" in lines
