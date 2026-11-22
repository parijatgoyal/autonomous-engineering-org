"""HTTP API + dashboard.

Every endpoint except sign-in requires a session (HttpOnly cookie, or `Authorization: Bearer <token>` for
scripts). Permissions are enforced in the orchestrator, so the CLI, API and UI follow the same rules.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import policy
from .config import ROOT
from .mock_provider import FAULTS
from .orchestrator import Orchestrator
from .users import AuthError, Forbidden, Sessions, User

app = FastAPI(title="Delivery Control Room", version="0.2.0")
orch = Orchestrator()
sessions = Sessions(orch.store)
STATIC = Path(__file__).parent / "static"
COOKIE = "aeeo_session"


def configure(o: Orchestrator) -> None:
    """Swap the orchestrator (used by tests)."""
    global orch, sessions
    orch, sessions = o, Sessions(o.store)


# ------------------------------------------------------------------ auth
def current_user(request: Request) -> User:
    token = request.cookies.get(COOKIE)
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:]
    try:
        return orch.users.get(sessions.resolve(token))
    except (AuthError, Forbidden) as e:
        raise HTTPException(401, str(e)) from e


CurrentUser = Annotated[User, Depends(current_user)]


class Login(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: Login, response: Response):
    try:
        user = orch.users.verify(body.username, body.password)
    except AuthError as e:
        raise HTTPException(401, str(e)) from e
    token = sessions.create(user.username)
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=Sessions.TTL)
    return {"user": user.public(), "token": token}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    if tok := request.cookies.get(COOKIE):
        sessions.revoke(tok)
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(user: CurrentUser):
    return user.public()


@app.get("/api/users")
def users(user: CurrentUser):
    return [u.public() for u in orch.users.all()]


# ------------------------------------------------------------------ runs
class RunRequest(BaseModel):
    requirement: str = Field(min_length=10, max_length=20000)
    repo: str = "shopkit"
    title: str | None = Field(default=None, max_length=120)
    faults: list[str] = []
    plan_review: bool = True
    max_cost_usd: float = Field(default=5.0, gt=0, le=100)


class Action(BaseModel):
    comment: str = Field(default="", max_length=4000)
    decision: str | None = None


def _bg(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def _guard(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except KeyError as e:
        raise HTTPException(404, f"not found: {e}") from e
    except Forbidden as e:
        raise HTTPException(403, str(e)) from e
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409, str(e)) from e


def _summary(run: dict, user: User) -> dict:
    s = run.get("state") or {}
    return {
        "id": run["id"], "title": run["title"], "status": run["status"], "stage": run["stage"],
        "created_by": run.get("created_by"), "created_at": run["created_at"], "updated_at": run["updated_at"],
        "iteration": s.get("iteration", 0), "rubric": (s.get("rubric") or {}).get("score"),
        "waiting_on": orch.pending(run)["waiting_on"], "my_actions": orch.actions_for(run, user.username),
    }


@app.get("/")
def dashboard():
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta(user: CurrentUser):
    return {
        "faults": FAULTS if orch.settings.provider == "mock" else {},
        "provider": orch.settings.provider, "model": orch.settings.model,
        "repositories": list(orch.repositories()),
        "sample_requirement": (ROOT / "requirements" / "discount_codes.md").read_text(),
        "limits": {"max_dev_iterations": orch.settings.max_dev_iterations,
                   "max_output_repairs": orch.settings.max_output_repairs,
                   "provider_retries": orch.settings.provider_retries},
        "can_create": user.role in ("requester", "engineer", "admin"),
        "can_audit": user.role in ("approver", "security", "admin"),
        "review_policy": policy.load_policy(ROOT),
    }


@app.get("/api/overview")
def overview(user: CurrentUser):
    return orch.overview()


@app.get("/api/runs")
def list_runs(user: CurrentUser):
    return [_summary(r, user) for r in orch.store.list_runs()]


@app.post("/api/runs", status_code=202)
def create_run(req: RunRequest, user: CurrentUser):
    if req.repo not in orch.repositories():
        raise HTTPException(400, f"unknown repository {req.repo!r}; choose one of {list(orch.repositories())}")
    run_id = _guard(orch.create_run, req.requirement, req.repo, created_by=user.username, faults=req.faults,
                    title=req.title, max_cost_usd=req.max_cost_usd, plan_review=req.plan_review)
    _bg(orch.execute, run_id)
    return {"run_id": run_id}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, user: CurrentUser):
    d = _guard(orch.details, run_id)
    d["my_actions"] = orch.actions_for(d, user.username)
    d["workspace_hint"] = f"git -C {d['state'].get('workspace')} log --oneline main..{d['state'].get('branch')}"
    return d


@app.get("/api/runs/{run_id}/patch", response_class=PlainTextResponse)
def get_patch(run_id: str, user: CurrentUser):
    text = _guard(orch.patch, run_id)
    return PlainTextResponse(text, headers={"content-disposition": f'attachment; filename="{run_id}.patch"'})


def _act(run_id: str, user: User, name: str, a: Action):
    """Run a human action; if it hands control back to the agents, continue in the background."""
    fn = {
        "confirm-plan": lambda: orch.confirm_plan(run_id, user.username, a.comment),
        "revise-plan": lambda: orch.revise_plan(run_id, user.username, a.comment),
        "signoff": lambda: orch.signoff(run_id, user.username, a.decision or "", a.comment),
        "approve": lambda: orch.approve(run_id, user.username, a.comment),
        "reject": lambda: orch.reject(run_id, user.username, a.comment),
        "request-changes": lambda: orch.request_changes(run_id, user.username, a.comment),
        "resolve": lambda: orch.resolve_conflict(run_id, user.username, a.decision or "", a.comment),
        "resume": lambda: orch.resume(run_id, user.username),
    }[name]
    result = _guard(fn)
    if orch.store.get_run(run_id)["status"] == "RUNNING":
        _bg(orch.execute, run_id)
    return result or {"ok": True}


@app.post("/api/runs/{run_id}/{action}")
def act(run_id: str, action: str, a: Action, user: CurrentUser):
    allowed = {"confirm-plan", "revise-plan", "signoff", "approve", "reject", "request-changes",
               "resolve", "resume"}
    if action not in allowed:
        raise HTTPException(404, "unknown action")
    return _act(run_id, user, action, a)


@app.get("/api/notifications")
def notifications(user: CurrentUser):
    mine = orch.notifier.for_user(user.username)
    return {"items": mine, "delivery": orch.notifier.mode(),
            "slack_linked": bool(orch.notifier.slack_ids.get(user.username) or orch.users.slack_id(user.username)),
            "all": orch.notifier.for_user(None) if user.role == "admin" else None}


@app.get("/api/audit")
def audit(user: CurrentUser):
    return _guard(orch.audit, user.username)
