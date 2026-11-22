"""The specialised agents. Each declares its responsibility, input and output contract.

| Agent      | Input                                      | Output            |
|------------|--------------------------------------------|-------------------|
| analyst    | requirement, repo overview                 | AnalysisOutput    |
| architect  | requirement, ACs, ranked code candidates   | DesignOutput      |
| architect_reconcile | design, developer concerns        | DesignReconciliation |
| developer  | design, ACs, current file contents, feedback | DevOutput       |
| qa         | ACs, design, diff                          | QAOutput          |
| security   | diff, deterministic scanner findings       | SecurityOutput    |
| release    | all evidence, rubric                       | ReleaseOutput     |
"""

from __future__ import annotations

import posixpath

from ..models import (
    AnalysisOutput,
    DesignOutput,
    DesignReconciliation,
    DevOutput,
    QAOutput,
    ReleaseOutput,
    SecurityOutput,
)
from .base import Agent


def _bad_path(p: str) -> bool:
    norm = posixpath.normpath(p)
    return p.startswith("/") or norm.startswith("..") or norm.startswith(".git") or norm != p


class AnalystAgent(Agent):
    name = "analyst"
    role = "Business Analyst"
    responsibilities = ("Turn a product requirement into testable Given/When/Then acceptance criteria, "
                        "a task plan, assumptions and open questions. You do not design or write code.")
    output_model = AnalysisOutput

    def instructions(self, ctx):
        return "Clarify the requirement into acceptance criteria (ids AC1..ACn) and a task plan."

    def semantic_check(self, out, ctx):
        ids = [ac.id for ac in out.acceptance_criteria]
        problems = []
        if len(set(ids)) != len(ids):
            problems.append("acceptance criteria ids must be unique")
        if len(ids) < 3:
            problems.append("need at least 3 acceptance criteria")
        return problems


class ArchitectAgent(Agent):
    name = "architect"
    role = "Software Architect"
    responsibilities = ("Inspect the ranked code candidates, identify impacted components and propose the "
                        "minimal design: which existing files change and which new files are created.")
    output_model = DesignOutput

    def instructions(self, ctx):
        return ("Using the code candidates (ranked by a static inspector) and file contents, identify impacted "
                "components and propose a design. files_to_modify must exist; files_to_create must not.")

    def semantic_check(self, out, ctx):
        existing = set(ctx.get("_repo_files", []))
        problems = []
        for p in out.files_to_modify:
            if p not in existing:
                problems.append(f"files_to_modify references non-existent file {p}")
        for p in out.files_to_create:
            if p in existing:
                problems.append(f"files_to_create references existing file {p}")
            if _bad_path(p):
                problems.append(f"unsafe path {p}")
        if not (out.files_to_modify or out.files_to_create):
            problems.append("design changes no files")
        return problems


class ArchitectReconcileAgent(Agent):
    name = "architect_reconcile"
    role = "Software Architect (design review)"
    responsibilities = ("Review the developer's objections to your design. Either accept and return a revised "
                        "design, or reject with rationale. Do not accept changes outside the requirement.")
    output_model = DesignReconciliation

    def semantic_check(self, out, ctx):
        if out.accept_developer_proposal and out.revised_design is None:
            return ["revised_design is required when accepting the proposal"]
        return []


class DeveloperAgent(Agent):
    name = "developer"
    role = "Software Engineer"
    responsibilities = ("Implement the approved design. Only edit files listed in the design. Return full file "
                        "contents. If the design is wrong, set agrees_with_design=false and explain instead of "
                        "deviating. Address all feedback from failed checks. Never edit tests written by QA.")
    output_model = DevOutput

    def instructions(self, ctx):
        return ("Implement the design. Each edit is the full new content of a file. If feedback is present, "
                "fix the root cause it describes.")

    def semantic_check(self, out, ctx):
        problems = [f"unsafe path {e.path}" for e in out.edits if _bad_path(e.path)]
        if out.agrees_with_design and not out.edits:
            problems.append("agrees with design but proposes no edits")
        paths = [e.path for e in out.edits]
        if len(set(paths)) != len(paths):
            problems.append("duplicate edit paths")
        return problems


class QAAgent(Agent):
    name = "qa"
    role = "QA Engineer"
    responsibilities = ("Write automated acceptance tests that verify each acceptance criterion through public "
                        "interfaces, and map every AC to the tests that cover it. Only write under tests/.")
    output_model = QAOutput

    def semantic_check(self, out, ctx):
        problems = [f"test file outside tests/: {f.path}" for f in out.test_files
                    if not f.path.startswith("tests/") or _bad_path(f.path)]
        ac_ids = {ac["id"] for ac in ctx.get("acceptance_criteria", [])}
        covered = {c.ac_id for c in out.ac_coverage if c.tests}
        missing = ac_ids - covered
        if missing:
            problems.append(f"acceptance criteria without tests: {sorted(missing)}")
        return problems


class SecurityAgent(Agent):
    name = "security"
    role = "Application Security Engineer"
    responsibilities = ("Review the diff and the deterministic scanner findings. Triage them, add issues the "
                        "scanners missed, and give a pass/block verdict. You cannot dismiss scanner findings "
                        "of high or critical severity.")
    output_model = SecurityOutput


class ReleaseAgent(Agent):
    name = "release"
    role = "Release Manager"
    responsibilities = ("Assemble the evidence (tests, checks, findings, rubric) into a concise review summary "
                        "with known risks and rollback plan for a human approver. You never approve or deploy.")
    output_model = ReleaseOutput


AGENTS = {a.name: a for a in (AnalystAgent(), ArchitectAgent(), ArchitectReconcileAgent(), DeveloperAgent(),
                               QAAgent(), SecurityAgent(), ReleaseAgent())}
