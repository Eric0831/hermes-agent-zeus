"""Central registry for all hermes-agent tools.

Each tool file calls ``registry.register()`` at module level to declare its
schema, handler, toolset membership, and availability check.  ``model_tools.py``
queries the registry instead of maintaining its own parallel data structures.

Import chain (circular-import safe):
    tools/registry.py  (no imports from model_tools or tool files)
           ^
    tools/*.py  (import from tools.registry at module level)
           ^
    model_tools.py  (imports tools.registry + all tool modules)
           ^
    run_agent.py, cli.py, batch_runner.py, etc.
"""

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class ToolSchemaValidationError(ValueError):
    """Raised when model-supplied tool arguments fail schema validation."""

    def __init__(self, details: list[str]):
        super().__init__("tool schema validation failed")
        self.details = details


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches_type(expected: str, value: Any) -> bool:
    actual = _json_type_name(value)
    if expected == "number":
        return actual in {"integer", "number"}
    return actual == expected


def _validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    if not schema:
        return []
    errors: list[str] = []
    expected_type = schema.get("type")
    if expected_type and not _matches_type(expected_type, value):
        return [f"{path}: expected {expected_type}, got {_json_type_name(value)}"]

    if expected_type == "object":
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        additional = schema.get("additionalProperties", True)
        if not isinstance(value, dict):
            return [f"{path}: expected object, got {_json_type_name(value)}"]
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key}: missing required field")
        if additional is False:
            allowed = set(properties.keys())
            for key in value.keys():
                if key not in allowed:
                    errors.append(f"{path}.{key}: unexpected field")
        for key, child_schema in properties.items():
            if key in value:
                errors.extend(_validate_schema(value[key], child_schema, f"{path}.{key}"))
        return errors

    if expected_type == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array, got {_json_type_name(value)}"]
        item_schema = schema.get("items")
        if item_schema:
            for idx, item in enumerate(value):
                errors.extend(_validate_schema(item, item_schema, f"{path}[{idx}]"))
        return errors

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: expected one of {schema['enum']}, got {value!r}")

    minimum = schema.get("minimum")
    if minimum is not None and isinstance(value, (int, float)) and value < minimum:
        errors.append(f"{path}: expected >= {minimum}, got {value}")

    return errors


class ToolEntry:
    """Metadata for a single registered tool."""

    __slots__ = (
        "name", "toolset", "schema", "handler", "check_fn",
        "requires_env", "is_async", "description", "emoji",
        "output_schema", "timeout_seconds", "error_map", "side_effect",
    )

    def __init__(self, name, toolset, schema, handler, check_fn,
                 requires_env, is_async, description, emoji,
                 output_schema, timeout_seconds, error_map, side_effect):
        self.name = name
        self.toolset = toolset
        self.schema = schema
        self.handler = handler
        self.check_fn = check_fn
        self.requires_env = requires_env
        self.is_async = is_async
        self.description = description
        self.emoji = emoji
        self.output_schema = output_schema or {}
        self.timeout_seconds = timeout_seconds
        self.error_map = error_map or {}
        self.side_effect = side_effect or "read_only"


class ToolRegistry:
    """Singleton registry that collects tool schemas + handlers from tool files."""

    def __init__(self):
        self._tools: Dict[str, ToolEntry] = {}
        self._toolset_checks: Dict[str, Callable] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        check_fn: Callable = None,
        requires_env: list = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        output_schema: dict | None = None,
        timeout_seconds: int | None = None,
        error_map: dict | None = None,
        side_effect: str = "read_only",
    ):
        """Register a tool.  Called at module-import time by each tool file."""
        existing = self._tools.get(name)
        if existing and existing.toolset != toolset:
            logger.warning(
                "Tool name collision: '%s' (toolset '%s') is being "
                "overwritten by toolset '%s'",
                name, existing.toolset, toolset,
            )
        self._tools[name] = ToolEntry(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            requires_env=requires_env or [],
            is_async=is_async,
            description=description or schema.get("description", ""),
            emoji=emoji,
            output_schema=output_schema or {},
            timeout_seconds=timeout_seconds,
            error_map=error_map or {},
            side_effect=side_effect,
        )
        if check_fn and toolset not in self._toolset_checks:
            self._toolset_checks[toolset] = check_fn

    # ------------------------------------------------------------------
    # Schema retrieval
    # ------------------------------------------------------------------

    def get_definitions(self, tool_names: Set[str], quiet: bool = False) -> List[dict]:
        """Return OpenAI-format tool schemas for the requested tool names.

        Only tools whose ``check_fn()`` returns True (or have no check_fn)
        are included.
        """
        result = []
        check_results: Dict[Callable, bool] = {}
        for name in sorted(tool_names):
            entry = self._tools.get(name)
            if not entry:
                continue
            if entry.check_fn:
                if entry.check_fn not in check_results:
                    try:
                        check_results[entry.check_fn] = bool(entry.check_fn())
                    except Exception:
                        check_results[entry.check_fn] = False
                        if not quiet:
                            logger.debug("Tool %s check raised; skipping", name)
                if not check_results[entry.check_fn]:
                    if not quiet:
                        logger.debug("Tool %s unavailable (check failed)", name)
                    continue
            result.append({"type": "function", "function": entry.schema})
        return result

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def dispatch(self, name: str, args: dict, **kwargs) -> str:
        """Execute a tool handler by name.

        * Async handlers are bridged automatically via ``_run_async()``.
        * All exceptions are caught and returned as ``{"error": "..."}``
          for consistent error format.
        """
        entry = self._tools.get(name)
        if not entry:
            return json.dumps({"error": f"Unknown tool: {name}"})
        try:
            parameters_schema = (entry.schema or {}).get("parameters") or {}
            errors = _validate_schema(args, parameters_schema)
            if errors:
                raise ToolSchemaValidationError(errors)
            if entry.is_async:
                from model_tools import _run_async
                result = _run_async(entry.handler(args, **kwargs))
            else:
                result = entry.handler(args, **kwargs)

            try:
                parsed = json.loads(result)
            except Exception:
                return json.dumps(
                    {
                        "error": "tool_output_not_json",
                        "tool": name,
                    },
                    ensure_ascii=False,
                )

            output_errors = _validate_schema(parsed, entry.output_schema or {})
            if output_errors:
                return json.dumps(
                    {
                        "error": "tool_output_validation_failed",
                        "tool": name,
                        "details": output_errors,
                    },
                    ensure_ascii=False,
                )
            return json.dumps(parsed, ensure_ascii=False)
        except ToolSchemaValidationError as exc:
            return json.dumps(
                {
                    "error": "schema_validation_failed",
                    "tool": name,
                    "details": exc.details,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("Tool %s dispatch error: %s", name, e)
            return json.dumps({"error": f"Tool execution failed: {type(e).__name__}: {e}"})

    # ------------------------------------------------------------------
    # Query helpers  (replace redundant dicts in model_tools.py)
    # ------------------------------------------------------------------

    def get_all_tool_names(self) -> List[str]:
        """Return sorted list of all registered tool names."""
        return sorted(self._tools.keys())

    def get_toolset_for_tool(self, name: str) -> Optional[str]:
        """Return the toolset a tool belongs to, or None."""
        entry = self._tools.get(name)
        return entry.toolset if entry else None

    def get_emoji(self, name: str, default: str = "⚡") -> str:
        """Return the emoji for a tool, or *default* if unset."""
        entry = self._tools.get(name)
        return (entry.emoji if entry and entry.emoji else default)

    def get_tool_to_toolset_map(self) -> Dict[str, str]:
        """Return ``{tool_name: toolset_name}`` for every registered tool."""
        return {name: e.toolset for name, e in self._tools.items()}

    def get_tool_runtime_contract(self, name: str) -> Optional[dict[str, Any]]:
        entry = self._tools.get(name)
        if not entry:
            return None
        return {
            "name": entry.name,
            "toolset": entry.toolset,
            "timeout_seconds": entry.timeout_seconds,
            "side_effect": entry.side_effect,
            "error_map": entry.error_map,
            "has_input_schema": bool((entry.schema or {}).get("parameters")),
            "has_output_schema": bool(entry.output_schema),
        }

    def is_toolset_available(self, toolset: str) -> bool:
        """Check if a toolset's requirements are met.

        Returns False (rather than crashing) when the check function raises
        an unexpected exception (e.g. network error, missing import, bad config).
        """
        check = self._toolset_checks.get(toolset)
        if not check:
            return True
        try:
            return bool(check())
        except Exception:
            logger.debug("Toolset %s check raised; marking unavailable", toolset)
            return False

    def check_toolset_requirements(self) -> Dict[str, bool]:
        """Return ``{toolset: available_bool}`` for every toolset."""
        toolsets = set(e.toolset for e in self._tools.values())
        return {ts: self.is_toolset_available(ts) for ts in sorted(toolsets)}

    def get_available_toolsets(self) -> Dict[str, dict]:
        """Return toolset metadata for UI display."""
        toolsets: Dict[str, dict] = {}
        for entry in self._tools.values():
            ts = entry.toolset
            if ts not in toolsets:
                toolsets[ts] = {
                    "available": self.is_toolset_available(ts),
                    "tools": [],
                    "description": "",
                    "requirements": [],
                }
            toolsets[ts]["tools"].append(entry.name)
            if entry.requires_env:
                for env in entry.requires_env:
                    if env not in toolsets[ts]["requirements"]:
                        toolsets[ts]["requirements"].append(env)
        return toolsets

    def get_toolset_requirements(self) -> Dict[str, dict]:
        """Build a TOOLSET_REQUIREMENTS-compatible dict for backward compat."""
        result: Dict[str, dict] = {}
        for entry in self._tools.values():
            ts = entry.toolset
            if ts not in result:
                result[ts] = {
                    "name": ts,
                    "env_vars": [],
                    "check_fn": self._toolset_checks.get(ts),
                    "setup_url": None,
                    "tools": [],
                }
            if entry.name not in result[ts]["tools"]:
                result[ts]["tools"].append(entry.name)
            for env in entry.requires_env:
                if env not in result[ts]["env_vars"]:
                    result[ts]["env_vars"].append(env)
        return result

    def check_tool_availability(self, quiet: bool = False):
        """Return (available_toolsets, unavailable_info) like the old function."""
        available = []
        unavailable = []
        seen = set()
        for entry in self._tools.values():
            ts = entry.toolset
            if ts in seen:
                continue
            seen.add(ts)
            if self.is_toolset_available(ts):
                available.append(ts)
            else:
                unavailable.append({
                    "name": ts,
                    "env_vars": entry.requires_env,
                    "tools": [e.name for e in self._tools.values() if e.toolset == ts],
                })
        return available, unavailable


# Module-level singleton
registry = ToolRegistry()
