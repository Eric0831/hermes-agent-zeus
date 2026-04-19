"""AgentEOS brain data models — shared dataclasses for triage, planning, verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ── Executive Controller ─────────────────────────────────────────


@dataclass
class TriageResult:
    """Output of Executive Controller triage."""

    decision: str  # 'direct_reply' | 'create_task'
    reason: str
    task_type: str = "general"
    priority: str = "medium"
    risk_level: str = "low"
    requires_approval: bool = False
    budget_tokens: Optional[int] = None
    budget_ms: Optional[int] = None


# ── Planner ───────────────────────────────────────────────────────


@dataclass
class SubtaskSpec:
    """A single subtask in a plan."""

    id: str
    description: str
    tool: Optional[str] = None
    depends_on: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "tool": self.tool,
            "depends_on": self.depends_on,
        }


@dataclass
class PlanSpec:
    """Structured plan output from Planner."""

    goal: str
    success_criteria: list[str]
    subtasks: list[SubtaskSpec] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    recommended_tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "success_criteria": self.success_criteria,
            "subtasks": [s.to_dict() for s in self.subtasks],
            "risks": self.risks,
            "recommended_tools": self.recommended_tools,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanSpec":
        subtasks = [
            SubtaskSpec(
                id=s.get("id", f"s{i}"),
                description=s.get("description", ""),
                tool=s.get("tool"),
                depends_on=s.get("depends_on", []),
            )
            for i, s in enumerate(data.get("subtasks", []))
        ]
        return cls(
            goal=data.get("goal", ""),
            success_criteria=data.get("success_criteria", []),
            subtasks=subtasks,
            risks=data.get("risks", []),
            recommended_tools=data.get("recommended_tools", []),
        )


# ── Verifier ──────────────────────────────────────────────────────


@dataclass
class CriterionResult:
    """Verification result for a single success criterion."""

    criterion_key: str
    description: str
    status: str  # 'met' | 'unmet' | 'skipped'
    evidence_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion_key": self.criterion_key,
            "description": self.description,
            "status": self.status,
            "evidence_summary": self.evidence_summary,
        }


@dataclass
class VerificationResult:
    """Output of Verifier."""

    status: str  # 'pass' | 'fail_retriable' | 'fail_non_retriable' | 'needs_human'
    criteria_results: list[CriterionResult] = field(default_factory=list)
    summary: str = ""
    missing_evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "criteria_results": [c.to_dict() for c in self.criteria_results],
            "summary": self.summary,
            "missing_evidence": self.missing_evidence,
        }


# ── Policy ────────────────────────────────────────────────────────


@dataclass
class PolicyDecision:
    """Output of Policy evaluation."""

    decision: str  # 'allow' | 'deny' | 'allow_with_approval' | 'sandbox_required'
    reason: str
    risk_level: str = "low"
