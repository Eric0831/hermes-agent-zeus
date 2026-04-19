import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "gateway-monitor"
_SPEC = importlib.util.spec_from_loader(
    "gateway_monitor",
    SourceFileLoader("gateway_monitor", str(_MODULE_PATH)),
)
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_MODULE)


def test_monitor_noop_when_healthy():
    decision = _MODULE.decide_action({"health": "healthy", "running": True}, {"restart_timestamps": []}, now=1000)
    assert decision["action"] == "noop"
    assert decision["reason"] == "healthy"


def test_monitor_restarts_on_timeout_when_budget_available():
    decision = _MODULE.decide_action(
        {
            "health": "degraded",
            "running": True,
            "last_agent_failure": {"error_code": "timeout"},
        },
        {"restart_timestamps": []},
        now=1000,
    )
    assert decision["action"] == "restart"
    assert decision["reason"] == "degraded:timeout"


def test_monitor_skips_restart_on_rate_limit():
    decision = _MODULE.decide_action(
        {
            "health": "degraded",
            "running": True,
            "last_agent_failure": {"error_code": "rate_limited"},
        },
        {"restart_timestamps": []},
        now=1000,
    )
    assert decision["action"] == "noop"
    assert decision["reason"] == "degraded:rate_limited"


def test_monitor_respects_restart_budget():
    decision = _MODULE.decide_action(
        {"health": "failed", "running": False},
        {"restart_timestamps": [100, 200, 300]},
        now=3500,
    )
    assert decision["action"] == "noop"
    assert decision["reason"] == "restart_budget_exhausted"


def test_monitor_decision_keeps_recent_restart_count():
    decision = _MODULE.decide_action(
        {
            "health": "degraded",
            "running": True,
            "last_agent_failure": {"error_code": "connection_reset"},
        },
        {"restart_timestamps": [100, 200]},
        now=300,
    )
    assert decision["action"] == "restart"
    assert len(decision["restart_timestamps"]) == 2


def test_monitor_script_contains_preflight_gate():
    body = _MODULE_PATH.read_text(encoding="utf-8")
    assert "run_preflight" in body
    assert "preflight_failed:" in body
    assert "last_preflight_check" in body
