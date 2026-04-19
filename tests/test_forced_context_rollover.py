import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.modules.setdefault("fire", types.SimpleNamespace())
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))

from run_agent import AIAgent


def _mock_response(content="ok", finish_reason="stop"):
    msg = SimpleNamespace(
        content=content,
        tool_calls=None,
        reasoning_content=None,
        reasoning=None,
    )
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    resp = SimpleNamespace(choices=[choice], model="test/model")
    resp.usage = None
    return resp


def _make_agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def test_413_uses_forced_rollover_when_standard_compression_stalls():
    agent = _make_agent()
    err_413 = Exception("Request entity too large")
    err_413.status_code = 413
    agent.client.chat.completions.create.side_effect = [
        err_413,
        _mock_response(content="Recovered after forced rollover"),
    ]

    unchanged_messages = [
        {"role": "user", "content": "previous question"},
        {"role": "assistant", "content": "previous answer"},
        {"role": "user", "content": "hello"},
    ]
    rolled_messages = [{"role": "user", "content": "compressed handoff"}]

    with (
        patch.object(agent, "_compress_context", return_value=(unchanged_messages, "same prompt")),
        patch.object(agent, "_force_context_rollover", return_value=(rolled_messages, "rolled prompt")) as mock_force,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(
            "hello",
            conversation_history=[
                {"role": "user", "content": "previous question"},
                {"role": "assistant", "content": "previous answer"},
            ],
        )

    mock_force.assert_called_once()
    assert result["completed"] is True
    assert result["final_response"] == "Recovered after forced rollover"


def test_force_context_rollover_uses_more_aggressive_tail_protection_and_restores_config():
    agent = _make_agent()
    compressor = agent.context_compressor
    compressor.protect_last_n = 60
    compressor.summary_target_ratio = 0.60
    compressor.tail_token_budget = int(compressor.threshold_tokens * compressor.summary_target_ratio)

    original_protect_last = compressor.protect_last_n
    original_ratio = compressor.summary_target_ratio
    original_tail_budget = compressor.tail_token_budget

    messages = []
    for i in range(10):
        messages.append({"role": "user", "content": f"user message {i} " * 20})
        messages.append({"role": "assistant", "content": f"assistant message {i} " * 20})

    with (
        patch.object(compressor, "_generate_summary", return_value="summary"),
        patch.object(agent, "_finalize_compressed_rollover", side_effect=lambda compressed, _system: (compressed, "rolled")),
    ):
        forced = agent._force_context_rollover(messages, "")

    assert forced is not None
    compressed, new_prompt = forced
    assert new_prompt == "rolled"
    assert len(compressed) < len(messages)
    assert compressor.protect_last_n == original_protect_last
    assert compressor.summary_target_ratio == original_ratio
    assert compressor.tail_token_budget == original_tail_budget
