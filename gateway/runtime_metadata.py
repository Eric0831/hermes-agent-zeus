"""Runtime metadata helpers for gateway startup diagnostics."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from agent.prompt_builder import DEFAULT_AGENT_IDENTITY
from hermes_cli.config import get_hermes_home, load_config


def _safe_git_sha(project_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def _configured_model(config: dict[str, Any]) -> str:
    model_cfg = config.get("model")
    if isinstance(model_cfg, dict):
        value = model_cfg.get("default") or model_cfg.get("name") or ""
    elif isinstance(model_cfg, str):
        value = model_cfg
    else:
        value = ""
    return str(value).strip() or "unknown"


def collect_runtime_metadata(project_root: Path | None = None) -> dict[str, str]:
    """Collect a stable startup fingerprint for logs/status."""
    project_root = project_root or Path(__file__).resolve().parents[1]
    hermes_home = get_hermes_home()
    config_path = hermes_home / "config.yaml"
    config = load_config() or {}
    config_hash = "missing"
    if config_path.exists():
        try:
            config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()[:12]
        except Exception:
            config_hash = "unreadable"

    prompt_version = f"identity-{_stable_hash({'identity': DEFAULT_AGENT_IDENTITY})}"
    return {
        "git_sha": _safe_git_sha(project_root),
        "config_hash": config_hash,
        "prompt_version": prompt_version,
        "model_name": _configured_model(config),
    }
