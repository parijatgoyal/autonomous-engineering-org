"""Users, roles, sessions and the permission matrix.

Who does what:
  requester  product owner: submits requirements, reviews the plan, can withdraw their own run
  engineer   unblocks escalations, sends work back with guidance, resumes paused runs
  security   triages findings and signs off in the review pipeline before release
  approver   tech lead: final approval to merge + deploy (never on a run they created)
  admin      operates the system: sees everything, can reject or resume any run

Demo users and passwords are seeded into <data_dir>/users.json on first start.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

ROLES = ("requester", "engineer", "security", "approver", "admin")

PERMISSIONS: dict[str, set[str]] = {
    "run:create": {"requester", "engineer", "admin"},
    "plan:review": {"requester", "approver", "admin"},  # the run's creator may always review its plan
    "run:request_changes": {"engineer", "security", "approver", "admin"},
    "run:resolve": {"engineer", "approver"},
    "run:resume": {"engineer", "admin"},
    "review:signoff": {"security", "engineer"},  # the specific role per gate comes from policy.py
    "run:approve": {"approver"},
    "run:reject": {"approver", "admin"},  # the creator may withdraw their own run
    "audit:view": {"approver", "security", "admin"},
}

DEMO_USERS = [
    {"username": "asha", "name": "Asha Rao", "role": "requester", "title": "Product Manager"},
    {"username": "dev", "name": "Dev Mehta", "role": "engineer", "title": "Senior Engineer"},
    {"username": "sam", "name": "Sam Iyer", "role": "security", "title": "Security Reviewer"},
    {"username": "priya", "name": "Priya Nair", "role": "approver", "title": "Tech Lead"},
    {"username": "admin", "name": "Ops Admin", "role": "admin", "title": "Platform Admin"},
]


class AuthError(Exception):
    """Not signed in / bad credentials."""


class Forbidden(Exception):
    """Signed in but not allowed."""


@dataclass(frozen=True)
class User:
    username: str
    name: str
    role: str
    title: str = ""

    def public(self) -> dict:
        return {"username": self.username, "name": self.name, "role": self.role, "title": self.title}


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()


class Directory:
    def __init__(self, path: Path):
        self.path = path
        if not path.exists():
            self._seed()
        self._users = {u["username"]: u for u in json.loads(path.read_text())}

    def _seed(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for u in DEMO_USERS:
            salt = secrets.token_hex(8)
            rows.append({**u, "slack_id": "", "salt": salt, "password_hash": _hash(f"{u['username']}-demo", salt)})
        self.path.write_text(json.dumps(rows, indent=2))
        self.path.chmod(0o600)

    def get(self, username: str) -> User:
        u = self._users.get((username or "").strip().lower())
        if not u:
            raise Forbidden(f"unknown user {username!r}")
        return User(u["username"], u["name"], u["role"], u.get("title", ""))

    def slack_id(self, username: str) -> str:
        return (self._users.get(username) or {}).get("slack_id", "")

    def all(self) -> list[User]:
        return [self.get(u) for u in self._users]

    def verify(self, username: str, password: str) -> User:
        u = self._users.get((username or "").strip().lower())
        if not u or not hmac.compare_digest(u["password_hash"], _hash(password or "", u["salt"])):
            raise AuthError("invalid username or password")
        return self.get(username)

    def authorize(self, username: str, perm: str, run: dict | None = None) -> User:
        user = self.get(username)
        allowed = user.role in PERMISSIONS[perm]
        is_creator = bool(run) and run.get("created_by") == user.username
        if perm in ("plan:review", "run:reject") and is_creator:
            allowed = True
        if not allowed:
            raise Forbidden(f"{user.name} ({user.role}) is not allowed to {perm.split(':')[1].replace('_', ' ')}")
        if perm == "run:approve" and is_creator:
            raise Forbidden("four-eyes rule: you cannot approve a run you requested")
        return user


class Sessions:
    """Opaque session tokens stored in SQLite, 12h lifetime."""

    TTL = 12 * 3600

    def __init__(self, store):
        self.store = store
        with store._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, username TEXT NOT NULL, "
                      "expires_at REAL NOT NULL)")

    def create(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.store._conn() as c:
            c.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
            c.execute("INSERT INTO sessions VALUES (?,?,?)", (token, username, time.time() + self.TTL))
        return token

    def resolve(self, token: str | None) -> str:
        if not token:
            raise AuthError("not signed in")
        with self.store._conn() as c:
            row = c.execute("SELECT username, expires_at FROM sessions WHERE token=?", (token,)).fetchone()
        if not row or row["expires_at"] < time.time():
            raise AuthError("session expired, please sign in again")
        return row["username"]

    def revoke(self, token: str) -> None:
        with self.store._conn() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (token,))
