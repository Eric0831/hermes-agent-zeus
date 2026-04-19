"""Tests for brain.planner — plan generation and parsing."""

import json
import pytest
from brain.planner import generate_plan, _parse_plan_json, _validate_and_cap
from brain.models import PlanSpec


class TestFallbackPlans:
    """Fallback plans when no LLM is available."""

    def test_general_fallback(self):
        plan = generate_plan("Do something")
        assert isinstance(plan, PlanSpec)
        assert plan.goal == "Do something"
        assert len(plan.success_criteria) >= 1
        assert len(plan.subtasks) >= 1

    def test_research_fallback(self):
        plan = generate_plan("Research topic", task_type="research")
        assert len(plan.subtasks) == 3
        assert plan.subtasks[0].tool == "web_search"

    def test_coding_fallback(self):
        plan = generate_plan("Fix bug", task_type="coding")
        assert len(plan.subtasks) == 3
        assert any(s.tool == "terminal" for s in plan.subtasks)

    def test_summary_fallback(self):
        plan = generate_plan("Summarize data", task_type="summary")
        assert len(plan.subtasks) == 2
        assert len(plan.success_criteria) == 2


class TestParsePlanJson:
    """JSON parsing from LLM output."""

    def test_clean_json(self):
        raw = '{"goal": "test", "success_criteria": ["a"], "subtasks": []}'
        data = _parse_plan_json(raw)
        assert data["goal"] == "test"

    def test_markdown_fenced(self):
        raw = '```json\n{"goal": "test", "success_criteria": ["a"]}\n```'
        data = _parse_plan_json(raw)
        assert data["goal"] == "test"

    def test_markdown_no_lang(self):
        raw = '```\n{"goal": "test", "success_criteria": ["a"]}\n```'
        data = _parse_plan_json(raw)
        assert data["goal"] == "test"

    def test_json_with_preamble(self):
        raw = 'Here is the plan:\n{"goal": "test", "success_criteria": ["a"]}'
        data = _parse_plan_json(raw)
        assert data["goal"] == "test"

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="Could not parse"):
            _parse_plan_json("this is not json at all")


class TestValidateAndCap:
    """Validation and safety caps on plan data."""

    def test_caps_criteria(self):
        data = {"goal": "test", "success_criteria": [f"c{i}" for i in range(10)]}
        plan = _validate_and_cap(data, "test")
        assert len(plan.success_criteria) == 5  # MAX_CRITERIA

    def test_caps_subtasks(self):
        data = {
            "goal": "test",
            "success_criteria": ["a"],
            "subtasks": [{"id": f"s{i}", "description": f"step {i}"} for i in range(12)],
        }
        plan = _validate_and_cap(data, "test")
        assert len(plan.subtasks) == 8  # MAX_SUBTASKS

    def test_caps_risks(self):
        data = {"goal": "test", "risks": [f"risk {i}" for i in range(7)]}
        plan = _validate_and_cap(data, "test")
        assert len(plan.risks) == 3  # MAX_RISKS

    def test_empty_criteria_gets_default(self):
        data = {"goal": "my goal", "success_criteria": []}
        plan = _validate_and_cap(data, "my goal")
        assert len(plan.success_criteria) == 1
        assert "my goal" in plan.success_criteria[0]

    def test_empty_subtasks_gets_default(self):
        data = {"goal": "my goal", "subtasks": []}
        plan = _validate_and_cap(data, "my goal")
        assert len(plan.subtasks) == 1

    def test_deduplicates_subtask_ids(self):
        data = {
            "goal": "test",
            "subtasks": [
                {"id": "s1", "description": "a"},
                {"id": "s1", "description": "b"},  # duplicate
            ],
        }
        plan = _validate_and_cap(data, "test")
        ids = [s.id for s in plan.subtasks]
        assert len(set(ids)) == len(ids)  # all unique

    def test_non_dict_subtasks_skipped(self):
        data = {"goal": "test", "subtasks": ["not a dict", {"id": "s1", "description": "ok"}]}
        plan = _validate_and_cap(data, "test")
        assert len(plan.subtasks) == 1
        assert plan.subtasks[0].id == "s1"

    def test_preserves_depends_on(self):
        data = {
            "goal": "test",
            "subtasks": [
                {"id": "s1", "description": "a"},
                {"id": "s2", "description": "b", "depends_on": ["s1"]},
            ],
        }
        plan = _validate_and_cap(data, "test")
        assert plan.subtasks[1].depends_on == ["s1"]


class TestLLMPlanGeneration:
    """Test plan generation with a mock LLM."""

    def test_with_mock_llm(self):
        mock_response = json.dumps({
            "goal": "Research Python ORMs",
            "success_criteria": [
                "At least 3 ORMs listed",
                "Each has pros and cons",
            ],
            "subtasks": [
                {"id": "s1", "description": "Search for ORMs", "tool": "web_search"},
                {"id": "s2", "description": "Compare", "depends_on": ["s1"]},
            ],
            "risks": ["Rate limiting on search"],
            "recommended_tools": ["web_search"],
        })

        def mock_llm(system, user):
            return mock_response

        plan = generate_plan("Research Python ORMs", llm_call=mock_llm)
        assert plan.goal == "Research Python ORMs"
        assert len(plan.success_criteria) == 2
        assert len(plan.subtasks) == 2
        assert plan.subtasks[0].tool == "web_search"

    def test_llm_failure_fallback(self):
        def bad_llm(system, user):
            raise RuntimeError("API down")

        plan = generate_plan("Research topic", task_type="research", llm_call=bad_llm)
        # Should get fallback plan, not crash
        assert isinstance(plan, PlanSpec)
        assert len(plan.success_criteria) >= 1

    def test_llm_bad_json_fallback(self):
        def bad_json_llm(system, user):
            return "I'm sorry, I can't generate JSON right now."

        plan = generate_plan("Test task", llm_call=bad_json_llm)
        assert isinstance(plan, PlanSpec)
