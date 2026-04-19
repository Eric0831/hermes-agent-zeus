import importlib.util
import sys
import time
import types
from unittest.mock import patch
from pathlib import Path


def _load_process_registry_module():
    original_modules = {name: sys.modules.get(name) for name in (
        "tools",
        "tools.environments",
        "tools.environments.local",
        "tools.registry",
        "tools.ansi_strip",
        "agent",
        "agent.task_state",
    )}

    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules["tools"] = tools_pkg

    env_pkg = types.ModuleType("tools.environments")
    env_pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules["tools.environments"] = env_pkg

    local_mod = types.ModuleType("tools.environments.local")
    local_mod._find_shell = lambda: "/bin/bash"
    local_mod._sanitize_subprocess_env = lambda env, env_vars=None: dict(env)
    sys.modules["tools.environments.local"] = local_mod

    agent_pkg = types.ModuleType("agent")
    agent_pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("agent", agent_pkg)

    task_state_mod = types.ModuleType("agent.task_state")

    class _Transition:
        def __init__(self, **payload):
            self._payload = payload

        def as_dict(self):
            return dict(self._payload)

    def _transition_task_state(**kwargs):
        return _Transition(
            task_id=kwargs["task_id"],
            from_state=kwargs["current_state"],
            to_state=kwargs["next_state"],
            reason=kwargs["reason"],
            request_id=kwargs.get("request_id", ""),
            tool_name=kwargs.get("tool_name", ""),
            attempt_no=kwargs.get("attempt_no", 0),
            timestamp="2026-03-29T00:00:00Z",
        )

    task_state_mod.transition_task_state = _transition_task_state
    sys.modules["agent.task_state"] = task_state_mod

    registry_mod = types.ModuleType("tools.registry")

    class _Registry:
        def register(self, *args, **kwargs):
            return None

    registry_mod.registry = _Registry()
    sys.modules["tools.registry"] = registry_mod

    ansi_mod = types.ModuleType("tools.ansi_strip")
    ansi_mod.strip_ansi = lambda text: text
    sys.modules["tools.ansi_strip"] = ansi_mod

    module_path = Path(__file__).resolve().parents[2] / "tools" / "process_registry.py"
    spec = importlib.util.spec_from_file_location("isolated_process_registry", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    for name, previous in original_modules.items():
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    return module


_MODULE = _load_process_registry_module()
ProcessRegistry = _MODULE.ProcessRegistry
ProcessSession = _MODULE.ProcessSession


def test_poll_reports_waiting_tool_from_output():
    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_waiting",
        command="python app.py",
        started_at=time.time(),
        output_buffer="Password: ",
        job_state="running",
    )
    registry._running[session.id] = session

    ansi_mod = types.ModuleType("tools.ansi_strip")
    ansi_mod.strip_ansi = lambda text: text
    with patch.dict(sys.modules, {"tools.ansi_strip": ansi_mod}):
        result = registry.poll(session.id)
    assert result["job_state"] == "waiting_tool"
    assert result["status"] == "running"
    assert result["state_history"][-1]["to_state"] == "waiting_tool"


def test_list_reports_completed_job_state():
    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_done",
        command="pytest -q",
        started_at=time.time(),
        exited=True,
        exit_code=0,
        job_state="completed",
    )
    registry._finished[session.id] = session

    result = registry.list_sessions()
    assert result[0]["job_state"] == "completed"
    assert result[0]["status"] == "exited"


def test_kill_transitions_to_aborted():
    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_abort",
        command="sleep 60",
        started_at=time.time(),
        job_state="running",
        pid=99999,
        env_ref=types.SimpleNamespace(execute=lambda *args, **kwargs: {"output": ""}),
    )
    registry._running[session.id] = session

    result = registry.kill_process(session.id)
    assert result["status"] == "killed"
    assert registry._finished[session.id].job_state == "aborted"
