import importlib.util
from pathlib import Path
from importlib.machinery import SourceFileLoader


_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "gateway-healthcheck"
_SPEC = importlib.util.spec_from_loader(
    "gateway_healthcheck",
    SourceFileLoader("gateway_healthcheck", str(_MODULE_PATH)),
)
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_MODULE)


def test_healthcheck_healthy_when_running_without_failure():
    health, exit_code = _MODULE.classify_health({"gateway_state": "running"}, 123)
    assert health == "healthy"
    assert exit_code == 0


def test_healthcheck_failed_for_hard_failure_code():
    health, exit_code = _MODULE.classify_health(
        {
            "gateway_state": "running",
            "last_agent_failure": {"error_code": "provider_4xx_non_retryable"},
        },
        123,
    )
    assert health == "failed"
    assert exit_code == 2


def test_healthcheck_degraded_for_timeout():
    health, exit_code = _MODULE.classify_health(
        {
            "gateway_state": "running",
            "last_agent_failure": {"error_code": "timeout"},
        },
        123,
    )
    assert health == "degraded"
    assert exit_code == 3


def test_healthcheck_failed_when_gateway_not_running():
    health, exit_code = _MODULE.classify_health({"gateway_state": "running"}, None)
    assert health == "failed"
    assert exit_code == 1
