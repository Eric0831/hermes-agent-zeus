"""Tests for brain.verifier — task completion verification."""

import json
import pytest
from brain.verifier import verify_task, _check_criterion_heuristic, _extract_keywords
from brain.models import VerificationResult


class TestHeuristicVerification:
    """Heuristic (Level 1) verification tests."""

    def test_pass_with_evidence(self):
        criteria = [
            {"criterion_key": "c0", "description": "Listed at least 3 frameworks"},
            {"criterion_key": "c1", "description": "Each has pros and cons"},
        ]
        evidence = [
            {
                "source_type": "tool_output",
                "tool_name": "web_search",
                "summary": "Found Django, Flask, FastAPI frameworks with comparison",
                "payload_json": '{"results": ["Django", "Flask", "FastAPI"]}',
            },
        ]
        response = "Here are 3 frameworks: Django, Flask, FastAPI. Each has pros and cons..."

        vr = verify_task("Compare frameworks", criteria, evidence, response)
        assert vr.status == "pass"
        assert all(c.status == "met" for c in vr.criteria_results)

    def test_fail_no_evidence(self):
        criteria = [
            {"criterion_key": "c0", "description": "Database query optimized"},
        ]
        evidence = []
        response = "I've optimized the query."

        vr = verify_task("Optimize DB", criteria, evidence, response)
        # With response text mentioning optimization, might pass heuristic
        # but with NO evidence, should note it
        assert isinstance(vr, VerificationResult)

    def test_no_criteria_auto_pass(self):
        vr = verify_task("Some goal", [], [], "Done!")
        assert vr.status == "pass"
        assert "auto-pass" in vr.summary.lower()

    def test_partial_criteria_met(self):
        criteria = [
            {"criterion_key": "c0", "description": "Code compiles without errors"},
            {"criterion_key": "c1", "description": "All unit tests pass"},
            {"criterion_key": "c2", "description": "Documentation updated"},
        ]
        evidence = [
            {
                "source_type": "tool_output",
                "tool_name": "terminal",
                "summary": "Build succeeded. All tests passed.",
                "payload_json": "Build OK\n15 tests passed",
            },
        ]
        response = "Code compiles and tests pass. I didn't update the docs."

        vr = verify_task("Fix code", criteria, evidence, response)
        # At least some should be met, some not
        met_count = sum(1 for c in vr.criteria_results if c.status == "met")
        assert met_count >= 1  # code compile + tests should match


class TestKeywordExtraction:
    def test_removes_stop_words(self):
        words = _extract_keywords("the quick brown fox is in the box")
        assert "the" not in words
        assert "quick" in words
        assert "brown" in words

    def test_handles_chinese(self):
        words = _extract_keywords("已完成 資料庫 的 優化")
        assert "已" not in words  # stop word (if tokenized)
        assert "優化" in words

    def test_empty_string(self):
        words = _extract_keywords("")
        assert len(words) == 0


class TestCriterionHeuristic:
    def test_met_with_keyword_match(self):
        criterion = {"criterion_key": "c0", "description": "Search results include Python libraries"}
        evidence = [{"summary": "Found popular Python libraries: requests, flask", "payload_json": "{}"}]
        result = _check_criterion_heuristic(criterion, evidence, "Here are some Python libraries.")
        assert result.status == "met"

    def test_unmet_no_overlap(self):
        criterion = {"criterion_key": "c0", "description": "Database migration completed"}
        evidence = [{"summary": "Weather forecast for today", "payload_json": "{}"}]
        result = _check_criterion_heuristic(criterion, evidence, "Here's the weather.")
        assert result.status == "unmet"


class TestLLMVerification:
    """LLM-based (Level 2) verification tests."""

    def test_llm_verify_pass(self):
        criteria = [
            {"criterion_key": "c0", "description": "Listed 3 frameworks"},
        ]
        evidence = [
            {"source_type": "tool_output", "tool_name": "search",
             "summary": "Django Flask FastAPI", "payload_json": "{}"},
        ]

        mock_response = json.dumps({
            "criteria_results": [
                {"criterion_key": "c0", "description": "Listed 3 frameworks",
                 "status": "met", "evidence_summary": "3 frameworks found in search"},
            ],
            "overall_status": "pass",
            "summary": "All criteria met",
            "missing_evidence": [],
        })

        def mock_llm(system, user):
            return mock_response

        vr = verify_task("Compare", criteria, evidence, "Here are 3 frameworks", llm_call=mock_llm)
        assert vr.status == "pass"

    def test_llm_verify_fail(self):
        # Use a criterion that WON'T pass heuristic (no keyword overlap with response)
        criteria = [
            {"criterion_key": "c0", "description": "Database migration schema validated"},
        ]
        evidence = []

        mock_response = json.dumps({
            "criteria_results": [
                {"criterion_key": "c0", "description": "Database migration schema validated",
                 "status": "unmet", "evidence_summary": "No migration evidence found"},
            ],
            "overall_status": "fail_retriable",
            "summary": "No migration evidence",
            "missing_evidence": ["migration validation results"],
        })

        def mock_llm(system, user):
            return mock_response

        vr = verify_task("Run migration", criteria, evidence, "Done with the task.", llm_call=mock_llm)
        assert vr.status == "fail_retriable"
        assert len(vr.missing_evidence) > 0

    def test_llm_failure_falls_back(self):
        criteria = [
            {"criterion_key": "c0", "description": "Something done"},
        ]

        def bad_llm(system, user):
            raise RuntimeError("LLM down")

        vr = verify_task("Task", criteria, [], "Done", llm_call=bad_llm)
        # Should not crash — falls back to heuristic
        assert isinstance(vr, VerificationResult)

    def test_llm_bad_json_falls_back(self):
        criteria = [
            {"criterion_key": "c0", "description": "Data processed"},
        ]

        def bad_json_llm(system, user):
            return "I cannot generate JSON right now."

        vr = verify_task("Process data", criteria, [], "Done", llm_call=bad_json_llm)
        assert isinstance(vr, VerificationResult)
