"""Identity Core — stable system identity that persists across sessions.

Provides a versioned identity profile that defines the system's:
- Mission and purpose
- Value hierarchy
- Behavioral constraints (immutable)
- Response style preferences (adjustable)
- Permission boundaries

Phase 1: reads from a YAML/JSON file (~/.hermes/identity.yaml).
Runtime cannot modify the identity — only human edits can change it.
The identity is injected into Planner and Verifier prompts.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────

DEFAULT_IDENTITY: dict[str, Any] = {
    "version": "1.0",
    "name": "Hermes",
    "mission": "Assist the user by completing tasks reliably, verifiably, and safely.",
    "values": [
        "accuracy over speed",
        "evidence over claims",
        "safety over convenience",
        "transparency over black-box behavior",
    ],
    "constraints": {
        "immutable": [
            "Never execute destructive operations without explicit approval",
            "Never fabricate evidence or tool outputs",
            "Never bypass the verification step for high-risk tasks",
            "Always preserve audit trail for task state transitions",
        ],
        "adjustable": {
            "response_style": "concise and technical",
            "language": "match user's language",
            "verbosity": "medium",
        },
    },
    "permissions": {
        "can_read_files": True,
        "can_write_files": True,
        "can_execute_commands": True,
        "can_send_messages": True,
        "can_access_network": True,
        "can_create_scheduled_tasks": True,
        "requires_approval_for_high_risk": True,
    },
}

_cached_identity: Optional[dict[str, Any]] = None
_cached_mtime: float = 0.0


# ── Public API ────────────────────────────────────────────────────


def get_identity() -> dict[str, Any]:
    """
    Load the current identity profile.

    Priority:
    1. ~/.hermes/identity.yaml (user-customized)
    2. Default identity (built-in)

    Returns a frozen copy — callers should not modify it.
    """
    global _cached_identity, _cached_mtime

    identity_path = _identity_path()
    if identity_path.exists():
        try:
            mtime = identity_path.stat().st_mtime
            if _cached_identity is not None and mtime == _cached_mtime:
                return _cached_identity.copy()

            import yaml
            with open(identity_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

            # Merge with defaults (user file can override selectively)
            identity = _merge_identity(DEFAULT_IDENTITY, data)
            _cached_identity = identity
            _cached_mtime = mtime
            logger.debug("Loaded identity from %s (version: %s)",
                         identity_path, identity.get("version"))
            return identity.copy()
        except Exception as e:
            logger.warning("Failed to load identity.yaml (%s), using defaults", e)

    return DEFAULT_IDENTITY.copy()


def get_identity_prompt() -> str:
    """
    Build a prompt fragment from the identity for injection into system prompts.

    This gives the Planner and agent context about who they are and what
    constraints they must follow.
    """
    identity = get_identity()

    parts = [
        f"[IDENTITY: {identity.get('name', 'Agent')}]",
        f"Mission: {identity.get('mission', '')}",
    ]

    values = identity.get("values", [])
    if values:
        parts.append("Values: " + "; ".join(values[:5]))

    constraints = identity.get("constraints", {})
    immutable = constraints.get("immutable", [])
    if immutable:
        parts.append("Hard constraints (never violate):")
        for c in immutable[:5]:
            parts.append(f"  - {c}")

    adjustable = constraints.get("adjustable", {})
    if adjustable:
        style = adjustable.get("response_style", "")
        if style:
            parts.append(f"Response style: {style}")

    return "\n".join(parts)


def get_immutable_constraints() -> list[str]:
    """Get the list of constraints that can never be violated."""
    identity = get_identity()
    return identity.get("constraints", {}).get("immutable", [])


def get_permissions() -> dict[str, bool]:
    """Get the current permission set."""
    identity = get_identity()
    return identity.get("permissions", DEFAULT_IDENTITY["permissions"])


def check_permission(permission: str) -> bool:
    """Check if a specific permission is granted."""
    perms = get_permissions()
    return perms.get(permission, False)


# ── Identity File Management ─────────────────────────────────────


def create_default_identity_file() -> Path:
    """Create a default identity.yaml if it doesn't exist."""
    path = _identity_path()
    if path.exists():
        return path

    try:
        import yaml
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(DEFAULT_IDENTITY, f,
                       default_flow_style=False, sort_keys=False,
                       allow_unicode=True)
        logger.info("Created default identity at %s", path)
    except Exception as e:
        logger.warning("Failed to create identity file: %s", e)

    return path


# ── Internal ──────────────────────────────────────────────────────


def _identity_path() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "identity.yaml"
    except ImportError:
        return Path.home() / ".hermes" / "identity.yaml"


def _merge_identity(defaults: dict, overrides: dict) -> dict:
    """Deep merge overrides into defaults."""
    result = defaults.copy()
    for key, value in overrides.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_identity(result[key], value)
        elif key in result and isinstance(result[key], list) and isinstance(value, list):
            result[key] = value  # Replace lists entirely (user's list wins)
        else:
            result[key] = value
    return result
