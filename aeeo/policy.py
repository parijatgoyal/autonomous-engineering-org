"""Role-based review pipeline.

Which humans must sign off before the approver can merge is policy, not code.
Edit `review_policy.json` in the project root (it overrides the default below) to add reviewers:

  {"id": "service_owner", "role": "engineer", "label": "Pricing service owner review",
   "when": "touches:services/pricing/*", "enabled": true}

`when` conditions:
  always             every run
  has_findings       any security finding remains (even low severity)
  touches:<glob>     the change modifies a file matching the glob (CODEOWNERS-style)
  iterations>N       the agents needed more than N development iterations

Sign-offs are collected in the listed order, each by a user with the given role
who is not the run's requester. The final approver gate (tech lead, four-eyes)
always comes last and is not configurable.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path

DEFAULT_POLICY = {
    "release_signoffs": [
        {"id": "security", "role": "security", "label": "Security sign-off", "when": "has_findings",
         "enabled": True},
        # Examples for future rollout; switch on in review_policy.json.
        {"id": "service_owner", "role": "engineer", "label": "Service owner review",
         "when": "touches:services/pricing/*", "enabled": False},
        {"id": "extra_engineering_review", "role": "engineer", "label": "Engineering review (agents struggled)",
         "when": "iterations>2", "enabled": False},
    ]
}


def load_policy(root: Path) -> dict:
    path = root / "review_policy.json"
    if path.exists():
        return json.loads(path.read_text())
    return DEFAULT_POLICY


def _matches(when: str, state: dict, changed_files: list[str]) -> bool:
    if when == "always":
        return True
    if when == "has_findings":
        return bool((state.get("security") or {}).get("findings"))
    if when.startswith("touches:"):
        pattern = when.split(":", 1)[1]
        return any(fnmatch.fnmatch(f, pattern) for f in changed_files)
    if when.startswith("iterations>"):
        return state.get("iteration", 0) > int(when.split(">", 1)[1])
    raise ValueError(f"unknown review condition: {when}")


def required_signoffs(policy: dict, state: dict, changed_files: list[str]) -> list[dict]:
    return [
        {k: g[k] for k in ("id", "role", "label", "when")}
        for g in policy.get("release_signoffs", [])
        if g.get("enabled", True) and _matches(g["when"], state, changed_files)
    ]
