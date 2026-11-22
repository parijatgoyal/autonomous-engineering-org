"""Typed contracts for every agent's input/output and for workflow state.

Every agent output is validated against one of these models before the
orchestrator acts on it. Anything that does not validate is treated as
malformed output (see agents/base.py).
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Analyst
# --------------------------------------------------------------------------
class AcceptanceCriterion(Strict):
    id: str = Field(description="Stable id like AC1, AC2")
    given: str
    when: str
    then: str


class Task(Strict):
    id: str
    title: str
    description: str
    owner_agent: Literal["architect", "developer", "qa", "security", "release"]


class AnalysisOutput(Strict):
    summary: str
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)
    tasks: list[Task] = Field(min_length=1)
    assumptions: list[str]
    open_questions: list[str]


# --------------------------------------------------------------------------
# Architect
# --------------------------------------------------------------------------
class ImpactedComponent(Strict):
    path: str
    reason: str
    change_type: Literal["modify", "create", "read_only"]


class DesignOutput(Strict):
    impacted_components: list[ImpactedComponent] = Field(min_length=1)
    design: str
    files_to_modify: list[str]
    files_to_create: list[str]
    test_strategy: str
    risks: list[str]


class DesignReconciliation(Strict):
    accept_developer_proposal: bool
    rationale: str
    revised_design: DesignOutput | None = None


# --------------------------------------------------------------------------
# Developer
# --------------------------------------------------------------------------
class FileEdit(Strict):
    path: str
    content: str = Field(description="Full new file content")


class DevOutput(Strict):
    agrees_with_design: bool
    design_concerns: list[str]
    proposed_files_to_create: list[str]
    edits: list[FileEdit]
    summary: str


# --------------------------------------------------------------------------
# QA
# --------------------------------------------------------------------------
class ACCoverage(Strict):
    ac_id: str
    tests: list[str] = Field(description="Test function names that verify this AC")


class QAOutput(Strict):
    test_plan: str
    test_files: list[FileEdit] = Field(min_length=1)
    ac_coverage: list[ACCoverage]


# --------------------------------------------------------------------------
# Security
# --------------------------------------------------------------------------
class Severity(str, Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    info = "info"


class SecurityFinding(Strict):
    severity: Severity
    title: str
    file: str
    line: int
    recommendation: str
    source: str


class SecurityOutput(Strict):
    findings: list[SecurityFinding]
    verdict: Literal["pass", "block"]
    summary: str


# --------------------------------------------------------------------------
# Release
# --------------------------------------------------------------------------
class ReleaseOutput(Strict):
    review_summary: str
    test_evidence: str
    known_risks: list[str]
    rollback_plan: str
    recommendation: Literal["approve", "request_changes"]


# --------------------------------------------------------------------------
# Workflow state
# --------------------------------------------------------------------------
class RunStatus(str, Enum):
    running = "RUNNING"
    plan_review = "PLAN_REVIEW"  # requester confirms acceptance criteria / answers open questions
    paused = "PAUSED"  # provider unavailable; resumable from checkpoint
    needs_human = "NEEDS_HUMAN"  # escalation: agents could not resolve
    awaiting_approval = "AWAITING_APPROVAL"
    deployed = "DEPLOYED"
    rejected = "REJECTED"
    failed = "FAILED"


class Stage(str, Enum):
    analysis = "analysis"
    architecture = "architecture"
    development = "development"
    qa = "qa"
    security = "security"
    release = "release"
    approval = "approval"
    done = "done"


STAGE_ORDER = [
    Stage.analysis,
    Stage.architecture,
    Stage.development,
    Stage.qa,
    Stage.security,
    Stage.release,
    Stage.approval,
    Stage.done,
]
