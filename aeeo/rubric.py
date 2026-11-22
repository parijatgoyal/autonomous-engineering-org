"""Score agent-generated work against a fixed rubric and the pre-change baseline."""

from __future__ import annotations


def evaluate(state: dict, changed_files: list[str], max_iterations: int) -> dict:
    analysis = state.get("analysis") or {}
    design = state.get("design") or {}
    tests = state.get("tests") or {}
    qa = state.get("qa") or {}
    baseline = state.get("baseline_tests") or {}
    sec = state.get("security") or {}

    cases = {c["name"]: c["status"] for c in tests.get("cases", [])}
    acs = analysis.get("acceptance_criteria", [])
    coverage = {c["ac_id"]: c["tests"] for c in qa.get("ac_coverage", [])}
    uncovered = [ac["id"] for ac in acs
                 if not any(cases.get(t) == "passed" for t in coverage.get(ac["id"], []))]

    baseline_passed = [c["name"] for c in baseline.get("cases", []) if c["status"] == "passed"]
    regressed = [n for n in baseline_passed if cases.get(n) != "passed"]

    planned = set(design.get("files_to_modify", [])) | set(design.get("files_to_create", []))
    unplanned = [f for f in changed_files if f not in planned and not f.startswith("tests/")]
    open_blocking = [f for f in sec.get("findings", []) if f["severity"] in ("critical", "high")]

    criteria = [
        ("acceptance_criteria_testable",
         len(acs) >= 3 and all(ac.get("given") and ac.get("when") and ac.get("then") for ac in acs),
         f"{len(acs)} Given/When/Then criteria"),
        ("every_ac_verified_by_passing_test", not uncovered and bool(acs),
         "all covered" if not uncovered else f"not verified: {uncovered}"),
        ("all_tests_pass", tests.get("ok", False), f"{tests.get('passed', 0)}/{tests.get('total', 0)} passed"),
        ("no_baseline_regressions", not regressed and bool(baseline_passed),
         f"{len(baseline_passed)} baseline tests still pass" if not regressed else f"regressed: {regressed}"),
        ("tests_added", tests.get("total", 0) > baseline.get("total", 0),
         f"{baseline.get('total', 0)} -> {tests.get('total', 0)} tests"),
        ("lint_clean", (state.get("lint") or {}).get("ok", False),
         f"{len((state.get('lint') or {}).get('issues', []))} lint issues"),
        ("no_open_high_or_critical_findings", not open_blocking, f"{len(open_blocking)} open"),
        ("changes_confined_to_design", not unplanned, "confined" if not unplanned else f"unplanned: {unplanned}"),
        ("within_iteration_budget", state.get("iteration", 0) <= max_iterations,
         f"{state.get('iteration', 0)}/{max_iterations} iterations"),
    ]
    items = [{"criterion": n, "passed": bool(ok), "evidence": ev} for n, ok, ev in criteria]
    score = round(100 * sum(i["passed"] for i in items) / len(items))
    return {"score": score, "items": items,
            "baseline": {"tests": baseline.get("total", 0), "passed": len(baseline_passed)}}
