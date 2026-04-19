from agent.task_state import can_transition, transition_task_state


def test_task_state_machine_allows_waiting_model_retrying_and_completion():
    assert can_transition("queued", "running") is True
    assert can_transition("running", "waiting_model") is True
    assert can_transition("waiting_model", "retrying") is True
    assert can_transition("retrying", "completed") is True


def test_task_state_machine_rejects_completed_back_to_running():
    assert can_transition("completed", "running") is False


def test_transition_task_state_records_required_fields():
    transition = transition_task_state(
        task_id="task-1",
        current_state="running",
        next_state="waiting_tool",
        reason="tool_request_started",
        request_id="req-1",
        tool_name="terminal",
        attempt_no=2,
    )
    payload = transition.as_dict()
    assert payload["task_id"] == "task-1"
    assert payload["from_state"] == "running"
    assert payload["to_state"] == "waiting_tool"
    assert payload["request_id"] == "req-1"
    assert payload["tool_name"] == "terminal"
    assert payload["attempt_no"] == 2
    assert payload["timestamp"]
