"""Tests for brain.executive — triage logic."""

import pytest
from brain.executive import triage


class TestWordBoundaryRegression:
    """Ensure English keyword matching uses word boundaries (no false positives)."""

    def test_detest_not_match_test(self):
        r = triage("I really detest that movie")
        assert r.decision == "direct_reply"

    def test_contest_not_match_test(self):
        r = triage("He won the contest easily")
        assert r.decision == "direct_reply"

    def test_manifest_not_match_fix(self):
        r = triage("Check the manifest file")
        assert r.decision == "direct_reply"

    def test_real_test_still_works(self):
        r = triage("Test the deployment pipeline for production readiness")
        assert r.decision == "create_task"


class TestTriageDirectReply:
    """Messages that should go through the direct reply path."""

    def test_trivial_input(self):
        r = triage("hi")
        assert r.decision == "direct_reply"
        assert r.reason == "trivial_input"

    def test_empty_input(self):
        r = triage("")
        assert r.decision == "direct_reply"

    def test_short_greeting(self):
        r = triage("Hello, how are you?")
        assert r.decision == "direct_reply"

    def test_simple_question_en(self):
        r = triage("What is the capital of France?")
        assert r.decision == "direct_reply"
        assert r.reason == "simple_question"

    def test_simple_question_zh(self):
        r = triage("什麼是 Python？")
        assert r.decision == "direct_reply"

    def test_how_does_question(self):
        r = triage("How does async/await work in Python?")
        assert r.decision == "direct_reply"

    def test_explain_question(self):
        r = triage("Explain the difference between TCP and UDP")
        assert r.decision == "direct_reply"

    def test_short_no_task(self):
        r = triage("Thanks for the help!")
        assert r.decision == "direct_reply"

    def test_slash_command(self):
        r = triage("/status")
        assert r.decision == "direct_reply"
        assert r.reason == "slash_command"


class TestTriageCreateTask:
    """Messages that should create a structured task."""

    def test_research_en(self):
        r = triage("Research the top 5 Python web frameworks and compare them")
        assert r.decision == "create_task"
        assert r.task_type == "research"

    def test_research_zh(self):
        r = triage("幫我研究三個 ORM 框架的優缺點")
        assert r.decision == "create_task"
        assert r.task_type == "research"

    def test_coding_task(self):
        r = triage("Fix the authentication bug in the login module")
        assert r.decision == "create_task"
        assert r.task_type == "coding"

    def test_coding_task_zh(self):
        r = triage("修改 API 端點的錯誤處理邏輯")
        assert r.decision == "create_task"
        assert r.task_type == "coding"

    def test_summary_task(self):
        r = triage("Summarize today's meeting notes and action items")
        assert r.decision == "create_task"
        assert r.task_type == "summary"

    def test_summary_task_zh(self):
        r = triage("整理今天的專案進度更新")
        assert r.decision == "create_task"
        assert r.task_type == "summary"

    def test_build_task(self):
        r = triage("Build a REST API for user management with CRUD endpoints")
        assert r.decision == "create_task"

    def test_implement_task(self):
        r = triage("Implement rate limiting for the API gateway")
        assert r.decision == "create_task"
        assert r.task_type == "coding"

    def test_analyze_task(self):
        r = triage("Analyze the database query performance and identify bottlenecks")
        assert r.decision == "create_task"
        assert r.task_type == "research"

    def test_multistep_indicators(self):
        r = triage("First download the data, then parse it, then generate a report")
        assert r.decision == "create_task"
        # May match task_intent or multistep — both are correct
        assert r.reason in ("task_intent_detected", "multistep_detected")

    def test_multistep_zh(self):
        r = triage("先查資料，再整理成表格，最後寄出報告")
        assert r.decision == "create_task"

    def test_long_input(self):
        r = triage("a " * 300)  # 600 chars
        assert r.decision == "create_task"
        assert r.reason == "complex_input"

    def test_media_input(self):
        r = triage("Process this image", has_media=True)
        assert r.decision == "create_task"


class TestTriageRiskLevels:
    """Risk level estimation."""

    def test_high_risk_deploy(self):
        r = triage("Deploy the application to production")
        assert r.decision == "create_task"
        assert r.risk_level == "high"
        assert r.requires_approval is True

    def test_high_risk_delete(self):
        r = triage("Investigate and delete all old user records from the database")
        assert r.decision == "create_task"
        assert r.risk_level == "high"

    def test_medium_risk_send(self):
        r = triage("Write a weekly report and send it to all team members")
        assert r.decision == "create_task"
        assert r.risk_level == "medium"

    def test_low_risk_research(self):
        r = triage("Research best practices for Python logging")
        assert r.decision == "create_task"
        assert r.risk_level == "low"

    def test_urgent_priority(self):
        r = triage("Fix the production bug urgently!")
        assert r.decision == "create_task"
        assert r.priority == "high"
