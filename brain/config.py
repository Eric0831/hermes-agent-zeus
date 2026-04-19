"""Brain configuration — read/write brain settings from config.yaml.

The brain is ENABLED by default. Users can disable it with:
  brain:
    enabled: false
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_cached_config: dict[str, Any] | None = None
_cached_mtime: float = 0.0


def _config_path() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "config.yaml"
    except ImportError:
        return Path.home() / ".hermes" / "config.yaml"


def _load_config() -> dict[str, Any]:
    """Load brain config from config.yaml with file-mtime caching."""
    global _cached_config, _cached_mtime

    path = _config_path()
    if not path.exists():
        return {}

    try:
        mtime = path.stat().st_mtime
        if _cached_config is not None and mtime == _cached_mtime:
            return _cached_config

        import yaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        _cached_config = data
        _cached_mtime = mtime
        return data
    except Exception as e:
        logger.debug("Failed to read config.yaml for brain config: %s", e)
        return {}


def is_brain_enabled() -> bool:
    """Check if the brain task-tracking system is enabled.

    Enabled by default. Disable with:
      brain:
        enabled: false
    Or env var: HERMES_BRAIN_ENABLED=0
    """
    # Env var override (highest priority)
    env = os.environ.get("HERMES_BRAIN_ENABLED")
    if env is not None:
        return env.lower() not in ("0", "false", "no", "off")

    cfg = _load_config()
    brain_cfg = cfg.get("brain", {})
    if isinstance(brain_cfg, dict):
        return brain_cfg.get("enabled", True) is not False
    if isinstance(brain_cfg, bool):
        return brain_cfg

    return True  # default: enabled


def get_brain_config() -> dict[str, Any]:
    """Get the full brain configuration section."""
    cfg = _load_config()
    brain_cfg = cfg.get("brain", {})
    if not isinstance(brain_cfg, dict):
        brain_cfg = {"enabled": bool(brain_cfg)}

    # Defaults
    defaults = {
        "enabled": True,
        "planner_model": None,        # None = use auxiliary client
        "verify_with_llm": False,     # Phase 0: heuristic only by default
        "max_retries": 2,
        "log_level": "info",
    }
    for k, v in defaults.items():
        brain_cfg.setdefault(k, v)

    return brain_cfg
