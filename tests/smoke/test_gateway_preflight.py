import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "gateway-preflight"
_SPEC = importlib.util.spec_from_loader(
    "gateway_preflight",
    SourceFileLoader("gateway_preflight", str(_MODULE_PATH)),
)
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_MODULE)


def test_preflight_ready_when_gateway_not_running():
    status, exit_code, issues, mismatches = _MODULE.classify_preflight(
        {"gateway_state": "unknown", "runtime_metadata": {}},
        None,
        {
            "git_sha": "abc123",
            "config_hash": "cfg123",
            "prompt_version": "identity-123",
            "model_name": "gpt-test",
        },
        {"ok": True, "missing": [], "non_executable": []},
    )
    assert status == "ready"
    assert exit_code == 0
    assert issues == ["gateway_not_running"]
    assert mismatches == {}


def test_preflight_failed_for_runtime_fingerprint_mismatch():
    status, exit_code, issues, mismatches = _MODULE.classify_preflight(
        {
            "gateway_state": "running",
            "runtime_metadata": {
                "git_sha": "oldsha",
                "config_hash": "cfg123",
                "prompt_version": "identity-123",
                "model_name": "gpt-test",
            },
        },
        123,
        {
            "git_sha": "newsha",
            "config_hash": "cfg123",
            "prompt_version": "identity-123",
            "model_name": "gpt-test",
        },
        {"ok": True, "missing": [], "non_executable": []},
    )
    assert status == "failed"
    assert exit_code == 2
    assert "runtime_mismatch:git_sha" in issues
    assert mismatches["git_sha"] == {"running": "oldsha", "desired": "newsha"}


def test_preflight_failed_for_invalid_desired_metadata():
    status, exit_code, issues, mismatches = _MODULE.classify_preflight(
        {"gateway_state": "running", "runtime_metadata": {}},
        123,
        {
            "git_sha": "unknown",
            "config_hash": "missing",
            "prompt_version": "identity-123",
            "model_name": "unknown",
        },
        {"ok": True, "missing": [], "non_executable": []},
    )
    assert status == "failed"
    assert exit_code == 2
    assert "invalid_desired_git_sha" in issues
    assert "invalid_desired_config_hash" in issues
    assert "invalid_desired_model_name" in issues
    assert mismatches == {}


def test_preflight_failed_for_missing_or_non_executable_scripts():
    status, exit_code, issues, mismatches = _MODULE.classify_preflight(
        {"gateway_state": "unknown", "runtime_metadata": {}},
        None,
        {
            "git_sha": "abc123",
            "config_hash": "cfg123",
            "prompt_version": "identity-123",
            "model_name": "gpt-test",
        },
        {"ok": False, "missing": ["gateway-up"], "non_executable": ["gateway-healthcheck"]},
    )
    assert status == "failed"
    assert exit_code == 2
    assert "missing_script:gateway-up" in issues
    assert "non_executable_script:gateway-healthcheck" in issues
    assert mismatches == {}


def test_render_human_summary_includes_issues_and_mismatches():
    rendered = _MODULE.render_human_summary(
        {
            "status": "failed",
            "running": True,
            "desired_runtime_metadata": {
                "git_sha": "newsha",
                "config_hash": "cfg123",
                "prompt_version": "identity-123",
                "model_name": "gpt-test",
            },
            "running_runtime_metadata": {
                "git_sha": "oldsha",
                "config_hash": "cfg123",
                "prompt_version": "identity-123",
                "model_name": "gpt-test",
            },
            "issues": ["runtime_mismatch:git_sha"],
            "mismatches": {"git_sha": {"running": "oldsha", "desired": "newsha"}},
            "scripts": {"missing": [], "non_executable": []},
        }
    )
    assert "gateway preflight: failed" in rendered
    assert "issues:" in rendered
    assert "- runtime_mismatch:git_sha" in rendered
    assert "- git_sha: running=oldsha desired=newsha" in rendered
