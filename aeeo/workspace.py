"""Isolated per-run workspace with its own git repository.

* The supplied repository is copied, never modified in place.
* Baseline is committed on `main`; all agent work happens on `agent/<run_id>`.
* Only a human approval merges the branch back into `main`.
* File writes go through a scope guard; commands run from an allowlist with a
  timeout and a scrubbed environment (no API keys / tokens leak to tools).
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

IGNORE = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".ruff_cache", "*.pyc", ".venv")
ALLOWED_TOOLS = {"pytest", "ruff", "bandit"}
GIT_ENV = {
    "GIT_AUTHOR_NAME": "delivery-agents", "GIT_AUTHOR_EMAIL": "agents@aeeo.local",
    "GIT_COMMITTER_NAME": "delivery-agents", "GIT_COMMITTER_EMAIL": "agents@aeeo.local",
}


class ScopeViolation(Exception):
    def __init__(self, paths: list[str], allowed: list[str]):
        self.paths = paths
        self.allowed = allowed
        super().__init__(f"edits outside approved scope: {paths}")


@dataclass
class CommandResult:
    cmd: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def _scrubbed_env() -> dict[str, str]:
    secret_markers = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    env = {k: v for k, v in os.environ.items() if not any(m in k.upper() for m in secret_markers)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


class Workspace:
    def __init__(self, root: Path, run_id: str):
        self.root = root
        self.run_id = run_id
        self.branch = f"agent/{run_id}"

    # ------------------------------------------------------------- setup
    @classmethod
    def create(cls, source: Path, run_dir: Path, run_id: str) -> "Workspace":
        root = run_dir / "repo"
        if root.exists():
            return cls(root, run_id)
        shutil.copytree(source, root, ignore=IGNORE)
        ws = cls(root, run_id)
        ws.git("init", "-q", "-b", "main")
        ws.git("add", "-A")
        ws.git("commit", "-q", "-m", "Baseline snapshot of supplied repository")
        ws.git("checkout", "-q", "-b", ws.branch)
        return ws

    def git(self, *args: str, check: bool = True) -> str:
        r = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True,
                           env={**_scrubbed_env(), **GIT_ENV}, timeout=60)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
        return r.stdout

    # ------------------------------------------------------------- files
    def list_files(self) -> list[str]:
        return sorted(self.git("ls-files").split())

    def read(self, rel: str) -> str | None:
        p = self._resolve(rel)
        return p.read_text() if p.exists() else None

    def _resolve(self, rel: str) -> Path:
        norm = posixpath.normpath(rel)
        if rel.startswith("/") or norm.startswith("..") or norm.split("/")[0] == ".git":
            raise ScopeViolation([rel], [])
        p = (self.root / norm).resolve()
        if not str(p).startswith(str(self.root.resolve()) + os.sep):
            raise ScopeViolation([rel], [])
        return p

    def check_scope(self, paths: list[str], allowed: set[str], protected: set[str] | None = None) -> None:
        protected = protected or set()
        bad = [p for p in paths if p not in allowed or p in protected]
        if bad:
            raise ScopeViolation(bad, sorted(allowed - protected))

    def write_files(self, edits: list[tuple[str, str]]) -> list[str]:
        """Write files atomically-as-a-set (scope must be checked before). Returns paths actually changed."""
        changed = []
        for rel, content in edits:
            p = self._resolve(rel)
            if p.exists() and p.read_text() == content:
                continue  # idempotent: identical content is not a change
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            changed.append(rel)
        return changed

    def commit(self, message: str) -> str | None:
        self.git("add", "-A")
        if not self.git("status", "--porcelain").strip():
            return None
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "--short", "HEAD").strip()

    def diff_vs_main(self) -> str:
        return self.git("diff", "main..." + self.branch)

    def changed_files(self) -> list[str]:
        return [p for p in self.git("diff", "--name-only", "main..." + self.branch).split() if p]

    def merge_to_main(self, approver: str) -> str:
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", self.branch, "-m", f"Merge {self.branch} (approved by {approver})")
        return self.git("rev-parse", "--short", "HEAD").strip()

    # ---------------------------------------------------------- commands
    def run_tool(self, tool: str, args: list[str], timeout: int = 120) -> CommandResult:
        if tool not in ALLOWED_TOOLS:
            raise PermissionError(f"tool {tool!r} is not allowlisted")
        cmd = [sys.executable, "-m", tool, *args]
        try:
            r = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True, timeout=timeout,
                               env=_scrubbed_env(), stdin=subprocess.DEVNULL)
            return CommandResult(cmd, r.returncode, r.stdout, r.stderr)
        except subprocess.TimeoutExpired as e:
            return CommandResult(cmd, -1, e.stdout or "", e.stderr or "", timed_out=True)


def changeset_hash(edits: list[tuple[str, str]]) -> str:
    h = hashlib.sha256()
    for path, content in sorted(edits):
        h.update(path.encode() + b"\0" + content.encode() + b"\0")
    return h.hexdigest()[:16]
