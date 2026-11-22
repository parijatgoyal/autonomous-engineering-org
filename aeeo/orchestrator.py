"""Workflow orchestrator: a persisted state machine over specialised agents.

    analysis -> architecture -> development -> qa -> security -> release -> approval -> done
                                    ^            |        |
                                    +-- failed --+--------+   (bounded by iteration budget)

Where autonomy stops (human required):
  * approval: only a named human can merge the agent branch and deploy.
  * NEEDS_HUMAN: unresolved architect/developer conflict, iteration budget
    exhausted, no progress (repeated identical change), persistent malformed
    agent output, cost budget exceeded.
  * PAUSED: provider unavailable after retries; resumable from the checkpoint.

The orchestrator - not the agents - decides transitions. Agents only return
validated data; deterministic checks (tests, lint, scanners, scope guard)
gate every transition.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from . import checks, inspector, policy, rubric
from .agents import AGENTS
from .agents.base import AgentOutputError, Runtime
from .config import ROOT, Settings
from .llm import ProviderUnavailable, make_provider
from .models import RunStatus, Stage
from .notifications import Notifier
from .store import Store
from .users import Directory, Forbidden
from .workspace import ScopeViolation, Workspace, changeset_hash


def _files(paths: list[str]) -> str:
    """Human-readable file list for event messages: 'a.py, b.py and c.py'."""
    names = list(paths) or ["no files"]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


ROLE_NOUN = {"security": "a security reviewer", "engineer": "an engineer", "approver": "a tech lead",
             "requester": "the requester", "admin": "an admin"}


class Escalate(Exception):
    def __init__(self, kind: str, message: str, data: dict | None = None):
        self.kind, self.message, self.data = kind, message, data or {}
        super().__init__(message)


@dataclass
class RunCtx:
    run_id: str
    requirement: str
    options: dict
    state: dict
    stage: str
    status: str
    ws: Workspace
    rt: Runtime
    report_dir: Path


class Orchestrator:
    def __init__(self, settings: Settings | None = None, store: Store | None = None,
                 notifier: Notifier | None = None):
        self.settings = settings or Settings()
        self.store = store or Store(self.settings.db_path)
        self.users = Directory(self.settings.data_dir / "users.json")
        self.notifier = notifier or Notifier(self.store)
        self._active: set[str] = set()
        self._lock = threading.Lock()

    # ========================================================== repositories
    def repositories(self) -> dict[str, str]:
        """Registered repositories. The UI/API can only target these, never an arbitrary server path."""
        return {"shopkit": str(ROOT / "sample_repo")}

    # ================================================================ public
    def create_run(self, requirement: str, repo: str | Path, *, created_by: str, faults: list[str] | None = None,
                   title: str | None = None, max_cost_usd: float = 5.0, plan_review: bool = False) -> str:
        user = self.users.authorize(created_by, "run:create")
        repo_path = Path(self.repositories().get(str(repo), repo)).resolve()
        if not repo_path.is_dir():
            raise ValueError(f"repository not found: {repo}")
        if not requirement.strip():
            raise ValueError("requirement is empty")
        title = title or next((ln.lstrip("# ").strip() for ln in requirement.splitlines() if ln.strip()), "run")
        options = {"faults": list(faults or []), "max_cost_usd": max_cost_usd, "plan_review": plan_review,
                   "provider": self.settings.provider, "model": self.settings.model}
        make_provider(self.settings.provider, model=self.settings.model, effort=self.settings.effort,
                      faults=options["faults"])  # validate faults/provider up front
        run_id = self.store.create_run(title[:120], requirement, str(repo_path), options, created_by=user.username)
        run_dir = self.settings.runs_dir / run_id
        ws = Workspace.create(repo_path, run_dir, run_id)
        baseline = checks.run_tests(ws, run_dir / "reports", "baseline")
        state = {
            "iteration": 0,
            "iteration_budget": self.settings.max_dev_iterations,
            "feedback": [],
            "plan_feedback": [],
            "clarifications": [],
            "changeset_hashes": [],
            "violations": [],
            "design_history": [],
            "negotiations": 0,
            "check_history": [],
            "commits": [],
            "baseline_tests": baseline,
            "branch": ws.branch,
            "workspace": str(ws.root),
        }
        self.store.save_checkpoint(run_id, status=RunStatus.running.value, stage=Stage.analysis.value, state=state)
        self.store.event(run_id, None, "run_created",
                         f"{user.name} submitted the requirement. Working on branch {ws.branch}; baseline "
                         f"{baseline['passed']}/{baseline['total']} tests passing",
                         data={"faults": options["faults"], "repo": str(repo_path)})
        if not baseline["ok"]:
            self.store.event(run_id, None, "baseline_failing", "Baseline test suite is not green; "
                             "regressions cannot be distinguished from pre-existing failures", level="warn")
        return run_id

    def start(self, requirement: str, repo: str | Path, **kw) -> str:
        run_id = self.create_run(requirement, repo, **kw)
        self.execute(run_id)
        return run_id

    def execute(self, run_id: str) -> str:
        with self._lock:
            if run_id in self._active:
                raise RuntimeError(f"run {run_id} is already executing")
            self._active.add(run_id)
        try:
            return self._execute(run_id)
        finally:
            with self._lock:
                self._active.discard(run_id)

    # ----------------------------------------------------------- plan gate
    def confirm_plan(self, run_id: str, actor: str, clarifications: str = "") -> None:
        rc = self._load(run_id)
        user = self.users.authorize(actor, "plan:review", self._row(rc))
        self._expect(rc, RunStatus.plan_review)
        if clarifications.strip():
            rc.state["clarifications"].append({"by": user.name, "text": clarifications.strip()})
        rc.state["plan_confirmed_by"] = user.name
        self.store.record_approval(run_id, "confirm_plan", user.username, clarifications)
        self.store.event(run_id, "analysis", "plan_confirmed",
                         f"{user.name} confirmed the acceptance criteria"
                         + (" and added clarifications" if clarifications.strip() else ""))
        self._save(rc, RunStatus.running.value, Stage.architecture.value)

    def revise_plan(self, run_id: str, actor: str, comment: str) -> None:
        rc = self._load(run_id)
        user = self.users.authorize(actor, "plan:review", self._row(rc))
        self._expect(rc, RunStatus.plan_review)
        if not comment.strip():
            raise ValueError("describe what is wrong with the plan")
        rc.state["plan_feedback"].append({"by": user.name, "text": comment.strip()})
        self.store.record_approval(run_id, "revise_plan", user.username, comment)
        self.store.event(run_id, "analysis", "plan_revision_requested", f"{user.name} asked for a revised plan: "
                         f"{comment}")
        self._save(rc, RunStatus.running.value, Stage.analysis.value)

    # ------------------------------------------------ role-based review gates
    def signoff(self, run_id: str, actor: str, signoff_id: str, comment: str = "") -> None:
        """Record one reviewer sign-off from the role-based review pipeline (see policy.py)."""
        rc = self._load(run_id)
        self._expect(rc, RunStatus.awaiting_approval)
        s = rc.state
        gate = next((g for g in s.get("required_signoffs", []) if g["id"] == signoff_id), None)
        if not gate:
            raise ValueError(f"no '{signoff_id}' sign-off is required for this run")
        if signoff_id in s.setdefault("signoffs", {}):
            raise ValueError(f"{gate['label']} was already given")
        user = self.users.get(actor)
        if user.role != gate["role"]:
            raise Forbidden(f"{gate['label']} must come from a {gate['role']}, not a {user.role}")
        if self._row(rc).get("created_by") == user.username:
            raise Forbidden("four-eyes rule: you cannot review a run you requested")
        s["signoffs"][signoff_id] = {"by": user.name, "username": user.username, "comment": comment,
                                     "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self.store.record_approval(run_id, f"signoff:{signoff_id}", user.username, comment)
        self.store.event(run_id, "approval", "signed_off",
                         f"{user.name}: {gate['label']}" + (f" ({comment})" if comment else ""))
        self._save(rc, rc.status, rc.stage)

    def missing_signoffs(self, state: dict) -> list[dict]:
        done = state.get("signoffs") or {}
        return [g for g in state.get("required_signoffs", []) if g["id"] not in done]

    # ------------------------------------------------------- release gate
    def approve(self, run_id: str, actor: str, comment: str = "") -> dict:
        rc = self._load(run_id)
        user = self.users.authorize(actor, "run:approve", self._row(rc))
        self._expect(rc, RunStatus.awaiting_approval)
        s = rc.state
        # Defence in depth: re-verify every gate at the moment of approval.
        if not (s.get("tests") or {}).get("ok"):
            raise ValueError("cannot approve: tests are not passing")
        if any(f["severity"] in ("critical", "high") for f in (s.get("security") or {}).get("findings", [])):
            raise ValueError("cannot approve: open high/critical security findings")
        if missing := self.missing_signoffs(s):
            raise ValueError("cannot approve yet: waiting for " + ", ".join(g["label"] for g in missing))
        merge_sha = rc.ws.merge_to_main(user.name)
        deployment = {
            "environment": "staging (simulated)", "merge_commit": merge_sha, "approved_by": user.name,
            "requested_by": self._row(rc).get("created_by"), "comment": comment,
            "signoffs": {k: v["by"] for k, v in (s.get("signoffs") or {}).items()},
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        (rc.report_dir.parent / "deployment.json").write_text(json.dumps(deployment, indent=2))
        s["deployment"] = deployment
        self.store.record_approval(run_id, "approve", user.username, comment)
        self.store.event(run_id, "approval", "approved",
                         f"Approved by {user.name}; merged {rc.ws.branch} -> main ({merge_sha}) and deployed to "
                         f"staging (simulated)")
        self._save(rc, RunStatus.deployed.value, Stage.done.value)
        return deployment

    def reject(self, run_id: str, actor: str, comment: str = "") -> None:
        rc = self._load(run_id)
        row = self._row(rc)
        user = self.users.authorize(actor, "run:reject", row)
        open_states = (RunStatus.awaiting_approval, RunStatus.needs_human, RunStatus.paused, RunStatus.plan_review)
        if rc.status not in {s.value for s in open_states}:
            raise ValueError(f"run is {rc.status}; cannot reject")
        verb = "Withdrawn" if row.get("created_by") == user.username else "Rejected"
        self.store.record_approval(run_id, verb.lower(), user.username, comment)
        self.store.event(run_id, rc.stage, "rejected",
                         f"{verb} by {user.name}: {comment or 'no comment'}. Branch left unmerged.")
        self._save(rc, RunStatus.rejected.value, Stage.done.value)

    def request_changes(self, run_id: str, actor: str, comment: str) -> None:
        """Send work back to the developer agent with human guidance and a fresh iteration budget."""
        rc = self._load(run_id)
        user = self.users.authorize(actor, "run:request_changes", self._row(rc))
        if rc.status not in (RunStatus.awaiting_approval.value, RunStatus.needs_human.value):
            raise ValueError(f"run is {rc.status}; cannot request changes")
        if (rc.state.get("escalation") or {}).get("kind") == "design_conflict":
            raise ValueError("a design conflict must be resolved by choosing a side")
        if not comment.strip():
            raise ValueError("a comment describing the requested change is required")
        s = rc.state
        s["feedback"].append(f"{user.name} ({user.role}) requested changes: {comment}")
        s["iteration_budget"] = s["iteration"] + self.settings.max_dev_iterations
        s.pop("escalation", None)
        s.pop("signoffs", None)
        self.store.record_approval(run_id, "request_changes", user.username, comment)
        self.store.event(run_id, rc.stage, "changes_requested", f"{user.name} requested changes: {comment}")
        self._save(rc, RunStatus.running.value, Stage.development.value)

    def resolve_conflict(self, run_id: str, actor: str, decision: str, comment: str = "") -> None:
        rc = self._load(run_id)
        user = self.users.authorize(actor, "run:resolve", self._row(rc))
        esc = rc.state.get("escalation") or {}
        if rc.status != RunStatus.needs_human.value or esc.get("kind") != "design_conflict":
            raise ValueError("no design conflict awaiting resolution")
        if decision not in ("architect", "developer"):
            raise ValueError("decision must be 'architect' or 'developer'")
        s = rc.state
        if decision == "developer":
            s["design_history"].append(s["design"])
            design = dict(s["design"])
            design["files_to_create"] = sorted(set(design["files_to_create"]) | set(esc["data"]["proposed_files"]))
            design["design"] += f" [Decision by {user.name}: adopt the developer's proposal. {comment}]"
            s["design"] = design
        else:
            s["design_final"] = True
            s["feedback"].append(f"Decision by {user.name}: implement the architect's design as-is. {comment}")
        s.pop("escalation", None)
        self.store.record_approval(run_id, f"resolve:{decision}", user.username, comment)
        self.store.event(run_id, "architecture", "conflict_resolved", f"{user.name} sided with the {decision}")
        self._save(rc, RunStatus.running.value, Stage.development.value)

    def resume(self, run_id: str, actor: str) -> None:
        rc = self._load(run_id)
        user = self.users.authorize(actor, "run:resume", self._row(rc))
        self._expect(rc, RunStatus.paused)
        opts = dict(rc.options)
        if "provider_outage" in opts["faults"]:
            opts["faults"] = [f for f in opts["faults"] if f != "provider_outage"]  # simulated recovery
            self.store.update_options(run_id, opts)
        self.store.event(run_id, rc.stage, "resumed", f"{user.name} resumed the run from the checkpoint at "
                         f"'{rc.stage}'")
        self._save(rc, RunStatus.running.value, rc.stage)

    # ------------------------------------------------- who needs to act now
    ACTIONS = {
        "confirm_plan": ("plan:review", "Confirm plan"),
        "revise_plan": ("plan:review", "Ask for a revised plan"),
        "signoff": ("review:signoff", "Review sign-off"),
        "approve": ("run:approve", "Approve merge & deploy"),
        "request_changes": ("run:request_changes", "Send back with guidance"),
        "resolve": ("run:resolve", "Resolve design conflict"),
        "resume": ("run:resume", "Resume"),
        "reject": ("run:reject", "Reject"),
    }

    def pending(self, run: dict) -> dict:
        """What the run is waiting for, in plain words, and which actions are open."""
        st, s = run["status"], run.get("state") or {}
        esc = (s.get("escalation") or {}).get("kind")
        missing = self.missing_signoffs(s)
        if st == RunStatus.plan_review.value:
            return {"actions": ["confirm_plan", "revise_plan", "reject"],
                    "waiting_on": "The requester to confirm the acceptance criteria and answer open questions"}
        if st == RunStatus.awaiting_approval.value:
            if missing:
                nxt = missing[0]
                return {"actions": ["signoff", "request_changes", "reject"], "signoff": nxt,
                        "waiting_on": f"{nxt['label']} from {ROLE_NOUN.get(nxt['role'], nxt['role'])}, "
                                      "then tech lead approval"}
            return {"actions": ["approve", "request_changes", "reject"],
                    "waiting_on": "A tech lead (not the requester) to approve the merge and deployment"}
        if st == RunStatus.needs_human.value:
            if esc == "design_conflict":
                return {"actions": ["resolve", "reject"],
                        "waiting_on": "An engineer or tech lead to settle the design disagreement"}
            return {"actions": ["request_changes", "reject"],
                    "waiting_on": "An engineer or tech lead to give the agents guidance"}
        if st == RunStatus.paused.value:
            return {"actions": ["resume", "reject"],
                    "waiting_on": "An engineer to resume once the model provider is back"}
        if st == RunStatus.running.value:
            return {"actions": [], "waiting_on": "Agents are working"}
        return {"actions": [], "waiting_on": ""}

    def actions_for(self, run: dict, username: str) -> list[str]:
        allowed = []
        pend = self.pending(run)
        for a in pend["actions"]:
            if a == "signoff":
                user = self.users.get(username)
                if user.role == pend["signoff"]["role"] and run.get("created_by") != user.username:
                    allowed.append(a)
                continue
            try:
                self.users.authorize(username, self.ACTIONS[a][0], run)
                allowed.append(a)
            except Forbidden:
                pass
        return allowed

    def overview(self) -> dict:
        runs = self.store.list_runs()
        by_status: dict[str, int] = {}
        for r in runs:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        done = [r for r in runs if r["status"] == RunStatus.deployed.value]
        cycle = [r["updated_at"] - r["created_at"] for r in done]
        return {"runs": len(runs), "by_status": by_status, "total_cost_usd": self.store.total_cost(),
                "avg_cycle_s": round(sum(cycle) / len(cycle), 1) if cycle else None}

    def audit(self, actor: str) -> dict:
        self.users.authorize(actor, "audit:view")
        return {"decisions": self.store.all_approvals(), "events": self.store.notable_events()}

    def patch(self, run_id: str) -> str:
        run = self.store.get_run(run_id)
        if not run:
            raise KeyError(run_id)
        ws = Workspace(Path(run["state"]["workspace"]), run_id)
        return ws.git("format-patch", "--stdout", "main.." + ws.branch) if run["status"] != \
            RunStatus.deployed.value else ws.git("format-patch", "--stdout", "HEAD^1..HEAD^2")

    def details(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        if not run:
            raise KeyError(run_id)
        ws = Workspace(Path(run["state"]["workspace"]), run_id) if run["state"].get("workspace") else None
        diff = ""
        if ws and ws.root.exists():
            try:
                diff = ws.diff_vs_main() if run["status"] != RunStatus.deployed.value else \
                    ws.git("diff", "HEAD^1", "HEAD")
            except RuntimeError:
                diff = ""
        creator = None
        if run.get("created_by"):
            try:
                creator = self.users.get(run["created_by"]).public()
            except Forbidden:
                creator = {"username": run["created_by"], "name": run["created_by"], "role": "?"}
        return {
            **run,
            "created_by_user": creator,
            "pending": self.pending(run),
            "events": self.store.events(run_id),
            "executions": self.store.executions(run_id),
            "usage": self.store.usage_summary(run_id),
            "approvals": self.store.approvals(run_id),
            "diff": diff,
        }

    # ============================================================== engine
    def _execute(self, run_id: str) -> str:
        rc = self._load(run_id)
        if rc.status != RunStatus.running.value:
            return rc.status
        try:
            while rc.status == RunStatus.running.value:
                stage = rc.stage
                rc.rt.stage = stage
                next_stage = getattr(self, f"_stage_{stage}")(rc)
                if next_stage != stage or stage == Stage.development.value:
                    self.store.event(run_id, stage, "transition", f"{stage} -> {next_stage}")
                status = rc.status
                if next_stage == Stage.approval.value:
                    status = RunStatus.awaiting_approval.value
                elif stage == Stage.analysis.value and rc.options.get("plan_review"):
                    status = RunStatus.plan_review.value  # requester confirms the plan before design starts
                self._save(rc, status, next_stage)
                self._check_cost(rc)
        except ProviderUnavailable as e:
            self.store.event(run_id, rc.stage, "paused",
                             f"Model provider unavailable after {self.settings.provider_retries} retries ({e}). "
                             "Run paused at checkpoint; resume when the provider recovers.", level="error")
            self._save(rc, RunStatus.paused.value, rc.stage)
        except (Escalate, AgentOutputError) as e:
            kind = e.kind if isinstance(e, Escalate) else "malformed_agent_output"
            data = e.data if isinstance(e, Escalate) else {}
            rc.state["escalation"] = {"kind": kind, "message": str(e), "data": data, "stage": rc.stage}
            self.store.event(run_id, rc.stage, "escalated", f"Needs human: {e}", level="error", data=data)
            self._save(rc, RunStatus.needs_human.value, rc.stage)
        except Exception as e:  # noqa: BLE001 - last-resort: never leave a run silently RUNNING
            self.store.event(run_id, rc.stage, "failed", f"Unexpected error: {e}", level="error",
                             data={"traceback": traceback.format_exc()[-4000:]})
            self._save(rc, RunStatus.failed.value, rc.stage)
        return rc.status

    # -------------------------------------------------------------- stages
    def _stage_analysis(self, rc: RunCtx) -> str:
        files = rc.ws.list_files()
        ctx = {"requirement": rc.requirement,
               "repo_overview": {"readme": rc.ws.read("README.md") or "", "files": files},
               "reviewer_feedback_on_previous_plan": [f["text"] for f in rc.state.get("plan_feedback", [])]}
        out = AGENTS["analyst"].run(rc.rt, f"analysis:{len(rc.state.get('plan_feedback', []))}", ctx)
        rc.state["analysis"] = out.model_dump(mode="json")
        self.store.event(rc.run_id, "analysis", "analysis_done",
                         f"{len(out.acceptance_criteria)} acceptance criteria, {len(out.tasks)} tasks")
        return Stage.architecture.value

    def _stage_architecture(self, rc: RunCtx) -> str:
        files = rc.ws.list_files()
        index = inspector.index_repo({f: rc.ws.read(f) or "" for f in files if f.endswith(".py")})
        ac_text = " ".join(f"{a['given']} {a['when']} {a['then']}" for a in rc.state["analysis"]["acceptance_criteria"])
        candidates = inspector.rank_components(rc.requirement + " " + ac_text, index)
        rc.state["candidates"] = candidates
        self.store.event(rc.run_id, "architecture", "code_inspected",
                         f"Indexed {len(index)} modules; top candidates: "
                         + ", ".join(f"{c['path']} ({c['score']})" for c in candidates[:3]))
        ctx = {
            "requirement": rc.requirement,
            "acceptance_criteria": rc.state["analysis"]["acceptance_criteria"],
            "candidates": candidates,
            "file_contents": {c["path"]: rc.ws.read(c["path"]) for c in candidates[:3]},
            "_repo_files": files,
        }
        out = AGENTS["architect"].run(rc.rt, "architecture", ctx)
        rc.state["design"] = out.model_dump(mode="json")
        self.store.event(rc.run_id, "architecture", "design_proposed",
                         f"Will modify {_files(out.files_to_modify)}"
                         + (f" and create {_files(out.files_to_create)}" if out.files_to_create else ""))
        return Stage.development.value

    def _stage_development(self, rc: RunCtx) -> str:
        s = rc.state
        if s["iteration"] >= s["iteration_budget"]:
            raise Escalate("iteration_budget_exhausted",
                           f"{s['iteration']} development iterations used without passing all gates",
                           {"last_feedback": s["feedback"][-1:] if s["feedback"] else []})
        s["iteration"] += 1
        i = s["iteration"]
        design = s["design"]
        planned = design["files_to_modify"] + design["files_to_create"]
        ctx = {
            "requirement": rc.requirement,
            "acceptance_criteria": s["analysis"]["acceptance_criteria"],
            "design": design,
            "design_is_final": bool(s.get("design_final")),
            "files": {p: rc.ws.read(p) for p in planned},
            "feedback": s["feedback"][-3:],
            "iteration": i,
        }
        step = f"development:{i}:d{len(s['design_history'])}"
        out = AGENTS["developer"].run(rc.rt, step, ctx)

        if not out.agrees_with_design:
            s["iteration"] -= 1  # design negotiation does not consume the implementation budget
            return self._negotiate(rc, out)

        edits = [(e.path, e.content) for e in out.edits]
        try:
            rc.ws.check_scope([p for p, _ in edits], set(planned), protected=set(s.get("qa_files", [])))
        except ScopeViolation as v:
            s["violations"].append({"iteration": i, "agent": "developer", "paths": v.paths})
            s["feedback"].append(
                f"Iteration {i} was rejected without applying any edit: you modified {v.paths}, which are outside "
                f"the approved design. Only these files may be edited: {sorted(set(planned))}.")
            self.store.event(rc.run_id, "development", "scope_violation",
                             f"Blocked edits outside the approved design ({_files(v.paths)}). Nothing was written.",
                             level="warn", data={"paths": v.paths, "allowed": sorted(set(planned))})
            return Stage.development.value

        h = changeset_hash(edits)
        if h in s["changeset_hashes"]:
            self.store.event(rc.run_id, "development", "repeat_action_blocked",
                             f"Developer resubmitted an identical changeset ({h}) that already failed; "
                             "stopping the loop instead of re-running it.", level="error")
            raise Escalate("no_progress", "Developer is repeating a change that already failed checks",
                           {"changeset": h, "last_feedback": s["feedback"][-1:]})
        s["changeset_hashes"].append(h)
        changed = rc.ws.write_files(edits)
        sha = rc.ws.commit(f"developer: {out.summary} (iteration {i})")
        s["commits"].append({"sha": sha, "by": "developer", "iteration": i, "files": changed})
        s["dev_summary"] = out.summary
        self.store.event(rc.run_id, "development", "code_changed",
                         f"Build attempt {i}: committed {sha} changing {_files(changed)}")
        return Stage.qa.value

    def _negotiate(self, rc: RunCtx, dev_out) -> str:
        s = rc.state
        s["negotiations"] += 1
        self.store.event(rc.run_id, "development", "design_disagreement",
                         "Developer disagrees with design: " + "; ".join(dev_out.design_concerns), level="warn",
                         data={"proposed_files_to_create": dev_out.proposed_files_to_create})
        conflict = {"developer": dev_out.design_concerns, "proposed_files": dev_out.proposed_files_to_create,
                    "design": s["design"]["design"]}
        if s.get("design_final") or s["negotiations"] > self.settings.max_design_negotiations:
            raise Escalate("design_conflict", "Architect and developer could not agree on the design",
                           {**conflict, "architect": "design already reviewed"})
        ctx = {"requirement": rc.requirement, "current_design": s["design"],
               "developer_concerns": dev_out.design_concerns,
               "developer_proposed_files_to_create": dev_out.proposed_files_to_create,
               "candidates": s.get("candidates", [])}
        rec = AGENTS["architect_reconcile"].run(rc.rt, f"reconcile:{s['negotiations']}", ctx)
        if not rec.accept_developer_proposal:
            raise Escalate("design_conflict", "Architect rejected the developer's design objection",
                           {**conflict, "architect": rec.rationale})
        s["design_history"].append(s["design"])
        s["design"] = rec.revised_design.model_dump(mode="json")
        self.store.event(rc.run_id, "architecture", "design_revised",
                         f"Architect accepted developer proposal: {rec.rationale}")
        return Stage.development.value

    def _stage_qa(self, rc: RunCtx) -> str:
        s = rc.state
        i = s["iteration"]
        if not s.get("qa"):
            ctx = {"acceptance_criteria": s["analysis"]["acceptance_criteria"], "design": s["design"],
                   "diff": rc.ws.diff_vs_main()[:20000]}
            out = AGENTS["qa"].run(rc.rt, "qa:tests", ctx)
            paths = [f.path for f in out.test_files]
            baseline_tests = {c["file"].replace(".", "/") + ".py" for c in s["baseline_tests"].get("cases", [])}
            try:
                rc.ws.check_scope(paths, {p for p in paths if p.startswith("tests/")}, protected=baseline_tests)
            except ScopeViolation as v:
                raise Escalate("qa_scope_violation", f"QA tried to write outside tests/ or modify baseline tests: "
                               f"{v.paths}", {"paths": v.paths}) from v
            rc.ws.write_files([(f.path, f.content) for f in out.test_files])
            sha = rc.ws.commit("qa: acceptance tests for " + ", ".join(c.ac_id for c in out.ac_coverage))
            s["commits"].append({"sha": sha, "by": "qa", "iteration": i, "files": paths})
            s["qa"] = out.model_dump(mode="json")
            s["qa_files"] = paths
            self.store.event(rc.run_id, "qa", "tests_written", f"Wrote acceptance tests in {_files(paths)}")

        tests = checks.run_tests(rc.ws, rc.report_dir, f"iter{i}")
        lint = checks.run_lint(rc.ws, rc.ws.changed_files())
        s["tests"], s["lint"] = tests, lint
        s["check_history"].append({"iteration": i, "tests_passed": tests["passed"], "tests_failed": tests["failed"],
                                   "lint_issues": len(lint["issues"])})
        if tests["ok"] and lint["ok"]:
            self.store.event(rc.run_id, "qa", "checks_passed",
                             f"Build attempt {i}: {tests['passed']}/{tests['total']} tests passed, lint clean")
            return Stage.security.value

        failing = [c for c in tests["cases"] if c["status"] == "failed"]
        lines = [f"- test {c['name']}: {c['message']}" for c in failing]
        lines += [f"- lint {x['file']}:{x['line']} {x['code']} {x['message']}" for x in lint["issues"]]
        if tests["timed_out"]:
            lines.append("- test run timed out")
        if not failing and not tests["ok"] and not tests["timed_out"]:
            lines.append("- test run errored: " + tests["output_tail"][-800:])
        s["feedback"].append(f"Iteration {i} failed checks:\n" + "\n".join(lines))
        self.store.event(rc.run_id, "qa", "check_failed",
                         f"Build attempt {i}: {tests['failed']} test(s) failed, {len(lint['issues'])} lint issue(s). "
                         "Sent back to the developer agent.", level="error",
                         data={"failing_tests": [c["name"] for c in failing], "lint": lint["issues"]})
        return Stage.development.value

    def _stage_security(self, rc: RunCtx) -> str:
        s = rc.state
        i = s["iteration"]
        scan = checks.run_security_scan(rc.ws, rc.ws.changed_files())
        ctx = {"diff": rc.ws.diff_vs_main()[:20000], "scanner_findings": scan["findings"]}
        out = AGENTS["security"].run(rc.rt, f"security:{i}", ctx)
        findings = list(scan["findings"])
        seen = {(f["file"], f["line"], f["severity"]) for f in findings}
        for f in out.findings:
            fd = f.model_dump(mode="json")
            if (fd["file"], fd["line"], fd["severity"]) not in seen:
                findings.append(fd)
        blocking = [f for f in findings if f["severity"] in ("critical", "high")]
        if out.verdict == "pass" and scan["blocking"]:
            self.store.event(rc.run_id, "security", "agent_overruled",
                             "Security agent said 'pass' but deterministic scanners found high/critical issues; "
                             "scanner result wins.", level="warn")
        s["security"] = {"findings": findings, "verdict": "block" if blocking else "pass", "summary": out.summary,
                         "bandit_ran": scan["bandit_ran"]}
        if blocking:
            s["feedback"].append(f"Iteration {i} blocked by security review:\n" + "\n".join(
                f"- [{f['severity']}] {f['title']} at {f['file']}:{f['line']} -> {f['recommendation']}"
                for f in blocking))
            self.store.event(rc.run_id, "security", "security_blocked",
                             f"{len(blocking)} blocking finding(s): "
                             + "; ".join(f"[{f['severity']}] {f['title']}" for f in blocking[:3]),
                             level="error", data={"findings": blocking})
            return Stage.development.value
        bandit = "yes" if scan["bandit_ran"] else "no"
        self.store.event(rc.run_id, "security", "security_passed",
                         f"No blocking findings ({len(findings)} total, bandit={bandit})")
        return Stage.release.value

    def _stage_release(self, rc: RunCtx) -> str:
        s = rc.state
        changed = rc.ws.changed_files()
        s["rubric"] = rubric.evaluate(s, changed, s["iteration_budget"])
        ctx = {
            "requirement": rc.requirement,
            "acceptance_criteria": s["analysis"]["acceptance_criteria"],
            "design": s["design"]["design"],
            "changed_files": changed,
            "iterations": s["iteration"],
            "check_history": s["check_history"],
            "tests": {k: s["tests"][k] for k in ("passed", "failed", "total")},
            "lint_ok": s["lint"]["ok"],
            "security_findings": s["security"]["findings"],
            "scope_violations": s["violations"],
            "rubric": s["rubric"],
        }
        out = AGENTS["release"].run(rc.rt, f"release:{s['iteration']}", ctx)
        s["release"] = out.model_dump(mode="json")
        # Role-based review pipeline: which human sign-offs this change needs (policy.py).
        s["required_signoffs"] = policy.required_signoffs(policy.load_policy(ROOT), s, changed)
        s.pop("signoffs", None)
        if s["required_signoffs"]:
            self.store.event(rc.run_id, "release", "review_pipeline",
                             "Required sign-offs: " + ", ".join(f"{g['label']} ({g['role']})"
                                                               for g in s["required_signoffs"]))
        self.store.event(rc.run_id, "release", "approval_requested",
                         f"Evidence assembled (rubric {s['rubric']['score']}/100, recommendation: "
                         f"{out.recommendation}). Waiting for human approval to merge and deploy.")
        return Stage.approval.value

    # ------------------------------------------------------------ helpers
    def _check_cost(self, rc: RunCtx) -> None:
        if rc.status != RunStatus.running.value:
            return
        spent = self.store.usage_summary(rc.run_id)["total"]["cost_usd"]
        if spent > rc.options.get("max_cost_usd", 5.0):
            raise Escalate("cost_budget_exceeded", f"Spent ${spent:.4f} > budget ${rc.options['max_cost_usd']}")

    @staticmethod
    def _expect(rc: RunCtx, status: RunStatus) -> None:
        if rc.status != status.value:
            raise ValueError(f"run is {rc.status}; this action needs {status.value}")

    def _row(self, rc: RunCtx) -> dict:
        return self.store.get_run(rc.run_id) or {}

    def _load(self, run_id: str) -> RunCtx:
        run = self.store.get_run(run_id)
        if not run:
            raise KeyError(f"run not found: {run_id}")
        provider = make_provider(run["options"].get("provider", self.settings.provider),
                                 model=run["options"].get("model", self.settings.model),
                                 effort=self.settings.effort, faults=run["options"].get("faults", []))
        rt = Runtime(run_id, self.store, provider, self.settings)
        run_dir = self.settings.runs_dir / run_id
        requirement = run["requirement"]
        if run["state"].get("clarifications"):
            requirement += "\n\nClarifications from the requester:\n" + "\n".join(
                f"- {c['text']}" for c in run["state"]["clarifications"])
        return RunCtx(run_id, requirement, run["options"], run["state"], run["stage"], run["status"],
                      Workspace(run_dir / "repo", run_id), rt, run_dir / "reports")

    def _save(self, rc: RunCtx, status: str, stage: str) -> None:
        rc.status, rc.stage = status, stage
        self.store.save_checkpoint(rc.run_id, status=status, stage=stage, state=rc.state)
        if status != RunStatus.running.value:
            self._notify(rc.run_id)

    # ------------------------------------------------------- notifications
    def _audience(self, run: dict, pend: dict) -> tuple[list, str]:
        """The people who own the next step (not everyone who technically could act)."""
        st, s = run["status"], run.get("state") or {}
        creator = run.get("created_by")
        people = [u for u in self.users.all() if u.username != creator]
        if st == RunStatus.plan_review.value:
            return [self.users.get(creator)] if creator else [], "confirm_plan"
        if st == RunStatus.awaiting_approval.value:
            if gate := pend.get("signoff"):
                return [u for u in people if u.role == gate["role"]], "signoff"
            return [u for u in people if u.role == "approver"], "approve"
        if st == RunStatus.needs_human.value:
            if (s.get("escalation") or {}).get("kind") == "design_conflict":
                return [u for u in people if u.role in ("engineer", "approver")], "resolve"
            return [u for u in people if u.role == "engineer"], "request_changes"
        if st == RunStatus.paused.value:
            return [u for u in people if u.role == "engineer"], "resume"
        return [], ""

    def _message(self, run: dict, action: str) -> tuple[str, str]:
        s, title = run.get("state") or {}, f"*{run['title']}*"
        tests = s.get("tests") or {}
        evidence = f"Tests {tests.get('passed', 0)}/{tests.get('total', 0)} passing"
        if (s.get("rubric") or {}).get("score") is not None:
            evidence += f", rubric {s['rubric']['score']}/100"
        if action == "confirm_plan":
            a = s.get("analysis") or {}
            q = len(a.get("open_questions", []))
            return (f"Please review the plan for {title}",
                    f"The analyst drafted {len(a.get('acceptance_criteria', []))} acceptance criteria"
                    + (f" and has {q} open question{'s' if q != 1 else ''} for you." if q else "."))
        if action == "signoff":
            gate = self.pending(run)["signoff"]
            n = len((s.get("security") or {}).get("findings", []))
            return (f"{gate['label']} needed on {title}",
                    f"{evidence}. {n} non-blocking finding{'s' if n != 1 else ''} to review.")
        if action == "approve":
            return (f"Ready for your approval: {title}", f"{evidence}. All required sign-offs are in.")
        if action == "resolve":
            return (f"Design decision needed on {title}",
                    "The architect and developer agents disagree and need someone to choose.")
        if action == "request_changes":
            esc = (s.get("escalation") or {}).get("message", "")
            return (f"The agents are stuck on {title}", f"{esc}. They need guidance to continue.")
        if action == "resume":
            return (f"{title} is paused", "The model provider was unavailable. Resume it when it's back.")
        return (title, "")

    def _notify(self, run_id: str) -> None:
        """Tell the people who now need to act. Never lets a notification problem break the workflow."""
        try:
            run = self.store.get_run(run_id)
            pend = self.pending(run)
            s = run.get("state") or {}
            gate = (pend.get("signoff") or {}).get("id", "")
            step = f"{run['status']}:{run['stage']}:{gate}:{s.get('iteration', 0)}:{len(s.get('plan_feedback', []))}"
            audience, action = self._audience(run, pend)
            for user in audience:
                headline, detail = self._message(run, action)
                self.notifier.notify(dedupe_key=f"{run_id}:{step}:{user.username}", run_id=run_id, user=user,
                                     kind=action, headline=headline, detail=detail,
                                     slack_id=self.users.slack_id(user.username))
            creator = run.get("created_by")
            if creator and run["status"] in (RunStatus.deployed.value, RunStatus.rejected.value):
                done = run["status"] == RunStatus.deployed.value
                dep = s.get("deployment") or {}
                closer = next((a["approver"] for a in reversed(self.store.approvals(run_id))), "someone")
                self.notifier.notify(
                    dedupe_key=f"{run_id}:outcome:{creator}", run_id=run_id, user=self.users.get(creator),
                    kind="shipped" if done else "closed",
                    headline=f"{'Shipped' if done else 'Closed'}: *{run['title']}*",
                    detail=(f"Approved by {dep.get('approved_by')} and deployed to {dep.get('environment')}."
                            if done else f"Closed by {closer}. The agents' branch was not merged."),
                    slack_id=self.users.slack_id(creator))
        except Exception as e:  # noqa: BLE001
            self.store.event(run_id, None, "notification_failed", f"Could not queue notifications: {e}",
                             level="warn")
