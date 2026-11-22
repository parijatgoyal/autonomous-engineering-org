"""Command line interface.

  python -m aeeo.cli run --by asha --faults buggy_first_impl   # requester submits the sample requirement
  python -m aeeo.cli show <run_id>
  python -m aeeo.cli signoff <run_id> --gate security --by sam  # role-based review pipeline
  python -m aeeo.cli approve <run_id> --by priya                # tech lead (never the requester)
  python -m aeeo.cli demo                              # every failure scenario, summary table
  python -m aeeo.cli serve                             # dashboard on http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import ROOT
from .mock_provider import FAULTS
from .orchestrator import Orchestrator
from .users import Forbidden

SAMPLE_REQ = ROOT / "requirements" / "discount_codes.md"
COLORS = {"error": "\033[31m", "warn": "\033[33m", "info": "\033[0m"}


def _print_run(o: Orchestrator, run_id: str, verbose: bool = True) -> None:
    d = o.details(run_id)
    s = d["state"]
    print(f"\nRun {run_id}  status={d['status']}  stage={d['stage']}  iterations={s.get('iteration')}")
    if verbose:
        t0 = d["events"][0]["created_at"] if d["events"] else 0
        for e in d["events"]:
            if e["kind"] == "transition":
                continue
            c = COLORS.get(e["level"], "")
            print(f"  {e['created_at'] - t0:6.2f}s {c}{e['level']:<5} {e['stage'] or '-':<12} {e['message']}\033[0m")
    u = d["usage"]["total"]
    print(f"  usage: {u['calls']} agent calls, {u['input_tokens']} in / {u['output_tokens']} out tokens, "
          f"${u['cost_usd']:.4f}, {u['latency_ms']} ms model latency, {u['failures']} failed attempts")
    if s.get("rubric"):
        print(f"  rubric: {s['rubric']['score']}/100")
        for i in s["rubric"]["items"]:
            print(f"    {'PASS' if i['passed'] else 'FAIL'}  {i['criterion']}: {i['evidence']}")
    if s.get("release"):
        r = s["release"]
        print(f"  release: {r['review_summary']}\n  evidence: {r['test_evidence']}")
        print("  known risks: " + "; ".join(r["known_risks"]))
    if s.get("escalation"):
        print(f"  ESCALATION [{s['escalation']['kind']}]: {s['escalation']['message']}")
    pend = d["pending"]
    if pend["waiting_on"] and d["status"] != "RUNNING":
        print(f"  waiting on: {pend['waiting_on']}")
        hint = {
            "confirm_plan": f"confirm-plan {run_id} --by <requester>",
            "signoff": f"signoff {run_id} --gate {(pend.get('signoff') or {}).get('id')} --by <reviewer>",
            "approve": f"approve {run_id} --by <tech lead>",
            "resolve": f"resolve {run_id} --decision developer|architect --by <engineer>",
            "request_changes": f"request-changes {run_id} --by <engineer> -m '...'",
            "resume": f"resume {run_id} --by <engineer>",
        }
        for a in pend["actions"]:
            if a in hint:
                print(f"  next: python -m aeeo.cli {hint[a]}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aeeo")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a requirement through the workflow")
    r.add_argument("--requirement", default=str(SAMPLE_REQ), help="path to requirement markdown")
    r.add_argument("--repo", default="shopkit", help="registered repository name or a local path")
    r.add_argument("--by", required=True, help="username of the requester (see `users`)")
    r.add_argument("--plan-review", action="store_true", help="pause for the requester to confirm the plan")
    r.add_argument("--faults", default="", help="comma separated: " + ", ".join(FAULTS))
    r.add_argument("--max-cost", type=float, default=5.0)
    sub.add_parser("list")
    sub.add_parser("users", help="list demo users and roles")
    sub.add_parser("show").add_argument("run_id")
    for name in ("approve", "reject", "request-changes", "confirm-plan", "revise-plan", "resume"):
        a = sub.add_parser(name)
        a.add_argument("run_id")
        a.add_argument("--by", required=True, help="username of the person acting")
        a.add_argument("-m", "--comment", default="")
    so = sub.add_parser("signoff", help="give a review sign-off from the role-based pipeline")
    so.add_argument("run_id")
    so.add_argument("--gate", required=True, help="sign-off id, e.g. security")
    so.add_argument("--by", required=True)
    so.add_argument("-m", "--comment", default="")
    rs = sub.add_parser("resolve", help="resolve an architect/developer design conflict")
    rs.add_argument("run_id")
    rs.add_argument("--decision", choices=["architect", "developer"], required=True)
    rs.add_argument("--by", required=True)
    rs.add_argument("-m", "--comment", default="")
    sub.add_parser("demo", help="run every scenario and print a summary")
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=8000)
    args = p.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("aeeo.api:app", host="127.0.0.1", port=args.port)
        return 0

    o = Orchestrator()
    try:
        if args.cmd == "run":
            faults = [f for f in args.faults.split(",") if f]
            rid = o.start(Path(args.requirement).read_text(), args.repo, created_by=args.by, faults=faults,
                          max_cost_usd=args.max_cost, plan_review=args.plan_review)
            _print_run(o, rid)
        elif args.cmd == "list":
            for row in o.store.list_runs():
                print(f"{row['id']}  {row['status']:<18} {row['stage']:<12} {row.get('created_by') or '-':<7} "
                      f"{row['title']}")
        elif args.cmd == "users":
            for u in o.users.all():
                print(f"{u.username:<7} {u.role:<10} {u.name} ({u.title})   password: {u.username}-demo")
        elif args.cmd == "signoff":
            o.signoff(args.run_id, args.by, args.gate, args.comment)
            _print_run(o, args.run_id, verbose=False)
        elif args.cmd == "confirm-plan":
            o.confirm_plan(args.run_id, args.by, args.comment)
            o.execute(args.run_id)
            _print_run(o, args.run_id)
        elif args.cmd == "revise-plan":
            o.revise_plan(args.run_id, args.by, args.comment)
            o.execute(args.run_id)
            _print_run(o, args.run_id)
        elif args.cmd == "show":
            _print_run(o, args.run_id)
        elif args.cmd == "approve":
            print(json.dumps(o.approve(args.run_id, args.by, args.comment), indent=2))
        elif args.cmd == "reject":
            o.reject(args.run_id, args.by, args.comment)
            _print_run(o, args.run_id, verbose=False)
        elif args.cmd == "request-changes":
            o.request_changes(args.run_id, args.by, args.comment)
            o.execute(args.run_id)
            _print_run(o, args.run_id)
        elif args.cmd == "resolve":
            o.resolve_conflict(args.run_id, args.by, args.decision, args.comment)
            o.execute(args.run_id)
            _print_run(o, args.run_id)
        elif args.cmd == "resume":
            o.resume(args.run_id, args.by)
            o.execute(args.run_id)
            _print_run(o, args.run_id)
        elif args.cmd == "demo":
            req = SAMPLE_REQ.read_text()
            print(f"{'scenario':<24} {'status':<18} {'iter':>4} {'rubric':>6}  outcome")
            for f in ["(none)", *FAULTS]:
                t = time.time()
                rid = o.start(req, "shopkit", created_by="asha", faults=[] if f == "(none)" else [f])
                d = o.details(rid)
                s = d["state"]
                outcome = (s.get("escalation") or {}).get("message") or \
                    next((e["message"] for e in reversed(d["events"]) if e["kind"] in
                          ("approval_requested", "paused")), "")
                rub = s.get("rubric", {}).get("score", "-")
                print(f"{f:<24} {d['status']:<18} {s['iteration']:>4} {rub!s:>6}  {outcome[:70]} "
                      f"({time.time() - t:.1f}s)")
    except (ValueError, KeyError, RuntimeError, Forbidden) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
