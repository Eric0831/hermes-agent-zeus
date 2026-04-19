import importlib.util
import json
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "registry.py"
_SPEC = importlib.util.spec_from_file_location("isolated_tools_registry", _MODULE_PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_MODULE)
ToolRegistry = _MODULE.ToolRegistry


def test_dispatch_rejects_schema_mismatch():
    registry = ToolRegistry()
    registry.register(
        name="demo_tool",
        toolset="demo",
        schema={
            "name": "demo_tool",
            "description": "demo",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "minimum": 1},
                    "mode": {"type": "string", "enum": ["fast", "slow"]},
                },
                "required": ["count", "mode"],
                "additionalProperties": False,
            },
        },
        handler=lambda args, **_: json.dumps({"ok": True}),
    )

    result = json.loads(registry.dispatch("demo_tool", {"count": "1", "extra": True}))
    assert result["error"] == "schema_validation_failed"
    assert any("count" in detail for detail in result["details"])
    assert any("mode" in detail for detail in result["details"])
    assert any("extra" in detail for detail in result["details"])


def test_dispatch_accepts_valid_payload():
    registry = ToolRegistry()
    registry.register(
        name="demo_tool",
        toolset="demo",
        schema={
            "name": "demo_tool",
            "description": "demo",
            "parameters": {
                "type": "object",
                "properties": {"count": {"type": "integer", "minimum": 1}},
                "required": ["count"],
            },
        },
        handler=lambda args, **_: json.dumps({"count": args["count"]}),
    )

    result = json.loads(registry.dispatch("demo_tool", {"count": 2}))
    assert result == {"count": 2}


def test_dispatch_rejects_non_json_tool_output():
    registry = ToolRegistry()
    registry.register(
        name="demo_tool",
        toolset="demo",
        schema={
            "name": "demo_tool",
            "description": "demo",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        handler=lambda args, **_: "not-json",
    )

    result = json.loads(registry.dispatch("demo_tool", {}))
    assert result["error"] == "tool_output_not_json"


def test_dispatch_rejects_output_schema_mismatch():
    registry = ToolRegistry()
    registry.register(
        name="demo_tool",
        toolset="demo",
        schema={
            "name": "demo_tool",
            "description": "demo",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        output_schema={
            "type": "object",
            "properties": {"status": {"type": "string", "enum": ["ok"]}},
            "required": ["status"],
            "additionalProperties": False,
        },
        handler=lambda args, **_: json.dumps({"status": "bad", "extra": True}),
    )

    result = json.loads(registry.dispatch("demo_tool", {}))
    assert result["error"] == "tool_output_validation_failed"
    assert any("status" in detail or "extra" in detail for detail in result["details"])
