from pathlib import Path


def test_gateway_ops_scripts_exist():
    repo = Path(__file__).resolve().parents[2]
    cli_wrappers = {
        "gateway-up",
        "gateway-stop",
        "gateway-status",
        "gateway-logs",
    }
    all_scripts = set(cli_wrappers) | {"gateway-healthcheck", "gateway-monitor", "gateway-preflight"}
    for name in all_scripts:
        path = repo / "scripts" / name
        assert path.exists(), f"missing script: {path}"
        body = path.read_text(encoding="utf-8")
        if name in cli_wrappers:
            assert "hermes_cli.main gateway" in body
            if name == "gateway-up":
                assert "gateway-preflight" in body
                assert "--human" in body
        elif name == "gateway-healthcheck":
            assert "read_runtime_status" in body
        elif name == "gateway-preflight":
            assert "collect_runtime_metadata" in body
        else:
            assert "decide_action" in body
