import json
from pathlib import Path


def test_stability_v1_docs_and_scripts_exist():
    repo = Path(__file__).resolve().parents[2]
    required_paths = [
        repo / "AGENTS.md",
        repo / "IMPLEMENT.md",
        repo / "DOCUMENTATION.md",
        repo / "README.md",
        repo / "scripts" / "hermes_up.sh",
        repo / "scripts" / "hermes_stop.sh",
        repo / "scripts" / "hermes_restart.sh",
        repo / "scripts" / "hermes_status.sh",
        repo / "scripts" / "hermes_logs.sh",
        repo / "scripts" / "hermes_smoke_test.sh",
    ]
    for path in required_paths:
        assert path.exists(), f"missing v1 artifact: {path}"


def test_stability_v1_schema_bundle_exists_and_disallows_additional_properties():
    repo = Path(__file__).resolve().parents[2]
    schema_names = [
        "plan_result.schema.json",
        "tool_selection.schema.json",
        "tool_input.schema.json",
        "tool_output.schema.json",
        "final_response.schema.json",
        "error_report.schema.json",
    ]
    for name in schema_names:
        payload = json.loads((repo / "schemas" / name).read_text(encoding="utf-8"))
        assert payload["type"] == "object"
        assert payload["additionalProperties"] is False


def test_implement_doc_tracks_milestones():
    repo = Path(__file__).resolve().parents[2]
    body = (repo / "IMPLEMENT.md").read_text(encoding="utf-8")
    assert "Milestone 1" in body
    assert "Milestone 5" in body
    assert "Validation Commands" in body
