from pathlib import Path

import pytest

from aeeo.config import ROOT, Settings
from aeeo.orchestrator import Orchestrator
from aeeo.users import Forbidden
from aeeo.workspace import Workspace

REQ = (ROOT / "requirements" / "discount_codes.md").read_text()
REPO = ROOT / "sample_repo"


@pytest.fixture
def orch(tmp_path):
    s = Settings(data_dir=tmp_path / "data", provider="mock")
    s.provider_backoff_s = 0.0
    return Orchestrator(settings=s)


def run(orch, *faults, **kw):
    rid = orch.start(REQ, REPO, created_by="asha", faults=list(faults), **kw)
    return rid, orch.details(rid)


def kinds(d):
    return [e["kind"] for e in d["events"]]


def test_happy_path_then_human_approval_merges(orch):
    rid, d = run(orch)
    assert d["status"] == "AWAITING_APPROVAL"
    s = d["state"]
    assert s["tests"]["ok"] and s["tests"]["total"] > s["baseline_tests"]["total"]
    assert s["rubric"]["score"] == 100
    assert "services/pricing/pricing.py" in [c["path"] for c in s["design"]["impacted_components"]]
    # Work lives on the agent branch; main is untouched until a human approves.
    ws = Workspace(Path(s["workspace"]), rid)
    assert "discount" not in ws.git("show", "main:services/pricing/pricing.py")
    # The remaining low finding must be accepted by security before the tech lead can approve.
    assert [g["id"] for g in s["required_signoffs"]] == ["security"]
    with pytest.raises(ValueError, match="Security sign-off"):
        orch.approve(rid, "priya")
    orch.signoff(rid, "sam", "security", "low risk, tracked as follow-up")
    dep = orch.approve(rid, "priya", "looks good")
    assert orch.store.get_run(rid)["status"] == "DEPLOYED"
    assert dep["merge_commit"]
    assert "discount_code" in ws.git("show", "main:services/pricing/pricing.py")
    # The supplied source repository is never modified.
    assert "discount" not in (REPO / "services/pricing/pricing.py").read_text()


def test_only_tech_lead_can_approve_and_never_their_own_run(orch):
    rid, _ = run(orch)
    orch.signoff(rid, "sam", "security")
    for name in ("release", "developer", "bot", "", "asha", "dev", "sam", "admin"):
        with pytest.raises(Forbidden):
            orch.approve(rid, name)
    rid2 = orch.start(REQ, REPO, created_by="admin")
    orch.signoff(rid2, "sam", "security")
    orch.users._users["admin"]["role"] = "approver"  # even an approver cannot approve their own request
    with pytest.raises(Forbidden, match="four-eyes"):
        orch.approve(rid2, "admin")


def test_signoff_requires_matching_role(orch):
    rid, _ = run(orch)
    for who in ("priya", "dev", "admin"):
        with pytest.raises(Forbidden):
            orch.signoff(rid, who, "security")
    with pytest.raises(ValueError):
        orch.signoff(rid, "sam", "not_a_gate")


def test_requester_cannot_be_approver_role_actions(orch):
    with pytest.raises(Forbidden):
        orch.create_run(REQ, REPO, created_by="priya")  # approvers review, they don't submit


def test_plan_review_gate_with_clarifications(orch):
    rid, d = run(orch, plan_review=True)
    assert d["status"] == "PLAN_REVIEW" and d["state"]["analysis"]["open_questions"]
    with pytest.raises(Forbidden):
        orch.confirm_plan(rid, "dev")
    orch.revise_plan(rid, "asha", "please also cover zero-value orders")
    orch.execute(rid)
    assert orch.store.get_run(rid)["status"] == "PLAN_REVIEW"
    orch.confirm_plan(rid, "asha", "No, emails are out of scope for now.")
    orch.execute(rid)
    d = orch.details(rid)
    assert d["status"] == "AWAITING_APPROVAL"
    assert "emails are out of scope" in d["state"]["clarifications"][0]["text"]
    analyst_steps = [e["step_key"] for e in d["executions"] if e["agent"] == "analyst"]
    assert analyst_steps == ["analysis:0", "analysis:1"]


def test_review_policy_adds_service_owner_gate(orch, monkeypatch):
    from aeeo import policy

    pol = {"release_signoffs": [
        {"id": "service_owner", "role": "engineer", "label": "Pricing owner", "when": "touches:services/pricing/*"},
        {"id": "inventory_owner", "role": "engineer", "label": "Inventory owner", "when": "touches:services/inv*"},
    ]}
    monkeypatch.setattr(policy, "load_policy", lambda root: pol)
    rid, d = run(orch)
    assert [g["id"] for g in d["state"]["required_signoffs"]] == ["service_owner"]
    assert orch.actions_for(d, "dev") == ["signoff", "request_changes"]
    assert "signoff" not in orch.actions_for(d, "sam")
    orch.signoff(rid, "dev", "service_owner")
    orch.approve(rid, "priya")


def test_audit_visible_to_reviewers_only(orch):
    run(orch)
    assert orch.audit("priya")["events"]
    with pytest.raises(Forbidden):
        orch.audit("asha")


def test_cannot_approve_before_ready(orch):
    rid, _ = run(orch, "disagreement_deadlock")
    with pytest.raises(ValueError):
        orch.approve(rid, "priya")


def test_failed_test_loops_back_to_developer(orch):
    _, d = run(orch, "buggy_first_impl")
    assert d["status"] == "AWAITING_APPROVAL"
    assert "check_failed" in kinds(d)
    hist = d["state"]["check_history"]
    assert hist[0]["tests_failed"] == 1 and hist[-1]["tests_failed"] == 0
    assert "test_ac3_code_matching_ignores_case_and_whitespace" in d["state"]["feedback"][0]


def test_critical_security_issue_blocks_then_fixed(orch):
    _, d = run(orch, "security_issue")
    assert "security_blocked" in kinds(d)
    assert d["status"] == "AWAITING_APPROVAL"
    assert not any(f["severity"] == "critical" for f in d["state"]["security"]["findings"])
    assert "eval(" not in d["diff"]


def test_persistent_security_issue_escalates_and_cannot_be_approved(orch):
    rid, d = run(orch, "security_persistent")
    assert d["status"] == "NEEDS_HUMAN"
    assert d["state"]["escalation"]["kind"] == "iteration_budget_exhausted"
    assert d["state"]["iteration"] == orch.settings.max_dev_iterations
    with pytest.raises(ValueError):
        orch.approve(rid, "priya")


def test_disagreement_resolved_by_negotiation(orch):
    _, d = run(orch, "disagreement")
    assert "design_disagreement" in kinds(d) and "design_revised" in kinds(d)
    assert d["state"]["design_history"]
    assert d["status"] == "AWAITING_APPROVAL"


def test_disagreement_deadlock_escalates_and_human_resolves(orch):
    rid, d = run(orch, "disagreement_deadlock")
    assert d["status"] == "NEEDS_HUMAN"
    assert d["state"]["escalation"]["kind"] == "design_conflict"
    with pytest.raises(Forbidden):
        orch.resolve_conflict(rid, "asha", "developer")
    orch.resolve_conflict(rid, "dev", "developer", "separate module is fine")
    orch.execute(rid)
    assert orch.store.get_run(rid)["status"] == "AWAITING_APPROVAL"


def test_malformed_output_is_repaired(orch):
    rid, d = run(orch, "malformed_output")
    assert d["status"] == "AWAITING_APPROVAL"
    statuses = [(e["agent"], e["status"]) for e in d["executions"] if e["agent"] == "analyst"]
    assert statuses == [("analyst", "malformed"), ("analyst", "ok")]


def test_provider_outage_pauses_and_resume_does_not_repeat_work(orch):
    rid, d = run(orch, "provider_outage")
    assert d["status"] == "PAUSED" and d["stage"] == "security"
    ok_before = [e for e in d["executions"] if e["status"] == "ok"]
    commits_before = len(d["state"]["commits"])
    with pytest.raises(Forbidden):
        orch.resume(rid, "asha")
    orch.resume(rid, "dev")
    orch.execute(rid)
    d2 = orch.details(rid)
    assert d2["status"] == "AWAITING_APPROVAL"
    ok_after = [e for e in d2["executions"] if e["status"] == "ok"]
    # Only security and release ran after resume; analysis/design/dev/qa were not re-executed.
    assert [e["agent"] for e in ok_after[len(ok_before):]] == ["security", "release"]
    assert len(d2["state"]["commits"]) == commits_before


def test_scope_violation_blocked_and_unrelated_file_untouched(orch):
    _, d = run(orch, "scope_violation")
    assert "scope_violation" in kinds(d)
    assert d["state"]["violations"][0]["paths"] == ["services/notifications/email.py"]
    assert "services/notifications/email.py" not in d["diff"]
    assert d["status"] == "AWAITING_APPROVAL"


def test_repeated_action_stops_loop(orch):
    _, d = run(orch, "stuck_developer")
    assert "repeat_action_blocked" in kinds(d)
    assert d["state"]["escalation"]["kind"] == "no_progress"


def test_request_changes_gives_new_budget(orch):
    rid, d = run(orch, "security_persistent")
    orch.request_changes(rid, "dev", "use a lookup table, not eval")
    run_ = orch.store.get_run(rid)
    assert run_["status"] == "RUNNING"
    assert run_["state"]["iteration_budget"] == 2 * orch.settings.max_dev_iterations


def test_usage_and_cost_tracked(orch):
    _, d = run(orch)
    t = d["usage"]["total"]
    assert t["calls"] >= 6 and t["cost_usd"] > 0 and t["input_tokens"] > 0
