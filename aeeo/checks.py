"""Deterministic quality gates: unit tests, lint and security scanning.

These are real tool runs inside the workspace. Agents can interpret results
but cannot overrule them: a failing test or a high/critical scanner finding
blocks release regardless of what any agent says.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .workspace import Workspace


def run_tests(ws: Workspace, report_dir: Path, label: str) -> dict:
    report_dir.mkdir(parents=True, exist_ok=True)
    junit = report_dir / f"junit-{label}.xml"
    r = ws.run_tool("pytest", ["-q", "-p", "no:cacheprovider", f"--junitxml={junit}", "--rootdir", "."])
    cases = []
    if junit.exists():
        for tc in ET.parse(junit).getroot().iter("testcase"):
            status = "passed"
            msg = ""
            for tag in ("failure", "error"):
                el = tc.find(tag)
                if el is not None:
                    status = "failed"
                    msg = (el.get("message") or "")[:400]
            if tc.find("skipped") is not None:
                status = "skipped"
            cases.append({"name": tc.get("name"), "file": tc.get("classname"), "status": status, "message": msg})
    passed = sum(c["status"] == "passed" for c in cases)
    failed = sum(c["status"] == "failed" for c in cases)
    return {
        "ok": r.returncode == 0 and failed == 0 and not r.timed_out,
        "passed": passed,
        "failed": failed,
        "total": len(cases),
        "cases": cases,
        "timed_out": r.timed_out,
        "output_tail": (r.stdout + r.stderr)[-3000:],
    }


LINT_RULES = "E4,E7,E9,F,B"  # pyflakes, syntax/runtime errors, bugbear


def run_lint(ws: Workspace, files: list[str]) -> dict:
    py = [f for f in files if f.endswith(".py")]
    if not py:
        return {"ok": True, "issues": [], "skipped": False}
    # --isolated: ignore any user/global ruff config so the gate is reproducible.
    r = ws.run_tool("ruff", ["check", "--isolated", "--no-cache", "--select", LINT_RULES,
                             "--output-format", "json", *py])
    if r.returncode not in (0, 1):
        return {"ok": False, "issues": [], "skipped": True, "error": r.stderr[-500:]}
    issues = [
        {"file": i["filename"].replace(str(ws.root) + "/", ""), "line": i["location"]["row"],
         "code": i["code"], "message": i["message"]}
        for i in json.loads(r.stdout or "[]")
    ]
    return {"ok": not issues, "issues": issues, "skipped": False}


# Rules that encode policy, not just style. Severity reflects organisational risk.
PATTERN_RULES = [
    (re.compile(r"\beval\s*\("), "critical", "Dynamic code evaluation (eval)",
     "Replace eval() with an explicit lookup table or parser."),
    (re.compile(r"\bexec\s*\("), "critical", "Dynamic code execution (exec)", "Remove exec()."),
    (re.compile(r"pickle\.loads?\("), "high", "Unsafe deserialisation (pickle)", "Use JSON or a safe format."),
    (re.compile(r"shell\s*=\s*True"), "high", "Shell injection risk (shell=True)", "Pass argv list, shell=False."),
    (re.compile(r"""(?i)(api[_-]?key|secret|password|token)\s*=\s*['"][^'"]{8,}['"]"""), "critical",
     "Hard-coded credential", "Load secrets from the environment or a secret manager."),
]


def run_security_scan(ws: Workspace, files: list[str]) -> dict:
    src = [f for f in files if f.endswith(".py") and not f.startswith("tests/")]
    findings = []
    for f in src:
        text = ws.read(f) or ""
        for lineno, line in enumerate(text.splitlines(), 1):
            for rx, sev, title, rec in PATTERN_RULES:
                if rx.search(line):
                    findings.append({"severity": sev, "title": title, "file": f, "line": lineno,
                                     "recommendation": rec, "source": "policy-scanner"})
    bandit_ran = False
    if src:
        r = ws.run_tool("bandit", ["-q", "-f", "json", *src])
        if r.stdout.strip().startswith("{"):
            bandit_ran = True
            for i in json.loads(r.stdout).get("results", []):
                findings.append({
                    "severity": i["issue_severity"].lower(), "title": f"{i['test_id']}: {i['issue_text']}",
                    "file": i["filename"], "line": i["line_number"],
                    "recommendation": i.get("more_info", ""), "source": "bandit",
                })
    blocking = [f for f in findings if f["severity"] in ("critical", "high")]
    return {"findings": findings, "blocking": blocking, "bandit_ran": bandit_ran, "files_scanned": src}
