"""Gateway STT config tests — honor stt.enabled: false from config.yaml."""

import importlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from gateway.config import GatewayConfig, load_gateway_config


def test_gateway_config_stt_disabled_from_dict_nested():
    config = GatewayConfig.from_dict({"stt": {"enabled": False}})
    assert config.stt_enabled is False


def test_load_gateway_config_bridges_stt_enabled_from_config_yaml(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.dump({"stt": {"enabled": False}}),
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    config = load_gateway_config()

    assert config.stt_enabled is False


def test_gateway_run_load_config_expands_tier_based_model(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    zeus_root = tmp_path / "zeus"
    hermes_home.mkdir()
    (zeus_root / "config" / "prod").mkdir(parents=True)

    (hermes_home / "config.yaml").write_text(
        yaml.dump(
            {
                "model_tier": "standard",
                "model": {},
                "compression": {"summary_tier": "fast"},
            }
        ),
        encoding="utf-8",
    )
    (zeus_root / "config" / "prod" / "llm.yaml").write_text(
        yaml.dump(
            {
                "models": {
                    "standard": {
                        "url": "http://localhost:8100/v1/chat/completions",
                        "model": "Qwen/Qwen3.5-27B-FP8",
                    },
                    "fast": {
                        "url": "http://localhost:8101/v1/chat/completions",
                        "model": "lovedheart/Qwen3.5-9B-FP8",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("ZEUS_ROOT", str(zeus_root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

    cfg = gateway_run._load_gateway_config()

    assert cfg["model"]["default"] == "Qwen/Qwen3.5-27B-FP8"
    assert cfg["model"]["base_url"] == "http://localhost:8100/v1"
    assert cfg["compression"]["summary_model"] == "lovedheart/Qwen3.5-9B-FP8"
    assert gateway_run._resolve_gateway_model(cfg) == "Qwen/Qwen3.5-27B-FP8"


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_skips_when_stt_disabled():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=False)

    with patch(
        "tools.transcription_tools.transcribe_audio",
        side_effect=AssertionError("transcribe_audio should not be called when STT is disabled"),
    ), patch(
        "tools.transcription_tools.get_stt_model_from_config",
        return_value=None,
    ):
        result = await runner._enrich_message_with_transcription(
            "caption",
            ["/tmp/voice.ogg"],
        )

    assert "transcription is disabled" in result.lower()
    assert "caption" in result


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_avoids_bogus_no_provider_message_for_backend_key_errors():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": False, "error": "VOICE_TOOLS_OPENAI_KEY not set"},
    ), patch(
        "tools.transcription_tools.get_stt_model_from_config",
        return_value=None,
    ):
        result = await runner._enrich_message_with_transcription(
            "caption",
            ["/tmp/voice.ogg"],
        )

    assert "No STT provider is configured" not in result
    assert "trouble transcribing" in result
    assert "caption" in result
