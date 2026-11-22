"""SQLite persistence for workflow state, agent execution history and events.

Tables
  runs              one row per requirement run; `state` is the JSON checkpoint
  agent_executions  every agent call: attempt, latency, tokens, cost, outcome
  events            append-only audit log (stage changes, checks, violations...)
  approvals         human decisions
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    requirement TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    options TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step_key TEXT NOT NULL,
    agent TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,          -- ok | malformed | provider_error | rejected
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    input_json TEXT,
    output_json TEXT,
    error TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_exec_run ON agent_executions(run_id, step_key);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    stage TEXT,
    kind TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_run ON events(run_id);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    approver TEXT NOT NULL,
    comment TEXT,
    created_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._lock = threading.RLock()
        with self._conn() as c:
            c.executescript(SCHEMA)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(runs)")}
            if "created_by" not in cols:  # migrate databases created before user accounts existed
                c.execute("ALTER TABLE runs ADD COLUMN created_by TEXT")

    @contextmanager
    def _conn(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    # ---------------------------------------------------------------- runs
    def create_run(self, title: str, requirement: str, repo_path: str, options: dict,
                   created_by: str | None = None) -> str:
        run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO runs (id, title, requirement, repo_path, status, stage, options, state, created_at, "
                "updated_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, title, requirement, repo_path, "RUNNING", "analysis",
                 json.dumps(options), json.dumps({}), now, now, created_by),
            )
        return run_id

    def get_run(self, run_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["options"] = json.loads(d["options"])
        d["state"] = json.loads(d["state"])
        return d

    def list_runs(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["options"] = json.loads(d["options"])
            d["state"] = json.loads(d["state"])
            out.append(d)
        return out

    def all_approvals(self, limit: int = 200) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT a.*, r.title FROM approvals a JOIN runs r ON r.id=a.run_id "
                             "ORDER BY a.id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def notable_events(self, limit: int = 200) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT e.*, r.title FROM events e JOIN runs r ON r.id=e.run_id "
                             "WHERE e.level != 'info' OR e.kind IN ('approved','rejected','run_created') "
                             "ORDER BY e.id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def total_cost(self) -> float:
        with self._conn() as c:
            row = c.execute("SELECT COALESCE(SUM(cost_usd),0) c FROM agent_executions").fetchone()
        return round(row["c"], 6)

    def save_checkpoint(self, run_id: str, *, status: str, stage: str, state: dict) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET status=?, stage=?, state=?, updated_at=? WHERE id=?",
                (status, stage, json.dumps(state), time.time(), run_id),
            )

    def update_options(self, run_id: str, options: dict) -> None:
        with self._conn() as c:
            c.execute("UPDATE runs SET options=? WHERE id=?", (json.dumps(options), run_id))

    # ----------------------------------------------------- agent executions
    def record_execution(self, run_id: str, step_key: str, agent: str, attempt: int, status: str,
                         *, model: str = "", input_tokens: int = 0, output_tokens: int = 0,
                         cost: float = 0.0, latency_ms: int = 0, input_obj: Any = None,
                         output_obj: Any = None, error: str | None = None) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO agent_executions
                (run_id, step_key, agent, attempt, status, model, input_tokens, output_tokens,
                 cost_usd, latency_ms, input_json, output_json, error, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, step_key, agent, attempt, status, model, input_tokens, output_tokens,
                 cost, latency_ms,
                 json.dumps(input_obj, default=str) if input_obj is not None else None,
                 json.dumps(output_obj, default=str) if output_obj is not None else None,
                 error, time.time()),
            )

    def completed_output(self, run_id: str, step_key: str) -> dict | None:
        """Idempotency: return the validated output of an already-completed step."""
        with self._conn() as c:
            row = c.execute(
                "SELECT output_json FROM agent_executions WHERE run_id=? AND step_key=? AND status='ok' "
                "ORDER BY id DESC LIMIT 1",
                (run_id, step_key),
            ).fetchone()
        return json.loads(row["output_json"]) if row else None

    def executions(self, run_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, step_key, agent, attempt, status, model, input_tokens, output_tokens, "
                "cost_usd, latency_ms, error, created_at FROM agent_executions WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def usage_summary(self, run_id: str) -> dict:
        with self._conn() as c:
            rows = c.execute(
                "SELECT agent, COUNT(*) calls, SUM(input_tokens) tin, SUM(output_tokens) tout, "
                "SUM(cost_usd) cost, SUM(latency_ms) latency, "
                "SUM(CASE WHEN status!='ok' THEN 1 ELSE 0 END) failures "
                "FROM agent_executions WHERE run_id=? GROUP BY agent ORDER BY MIN(id)",
                (run_id,),
            ).fetchall()
        per_agent = [dict(r) for r in rows]
        total = {
            "calls": sum(r["calls"] for r in per_agent),
            "input_tokens": sum(r["tin"] or 0 for r in per_agent),
            "output_tokens": sum(r["tout"] or 0 for r in per_agent),
            "cost_usd": round(sum(r["cost"] or 0 for r in per_agent), 6),
            "latency_ms": sum(r["latency"] or 0 for r in per_agent),
            "failures": sum(r["failures"] or 0 for r in per_agent),
        }
        return {"per_agent": per_agent, "total": total}

    # --------------------------------------------------------------- events
    def event(self, run_id: str, stage: str | None, kind: str, message: str,
              level: str = "info", data: Any = None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO events (run_id, stage, kind, level, message, data, created_at) VALUES (?,?,?,?,?,?,?)",
                (run_id, stage, kind, level, message,
                 json.dumps(data, default=str) if data is not None else None, time.time()),
            )

    def events(self, run_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"]) if d["data"] else None
            out.append(d)
        return out

    # ------------------------------------------------------------ approvals
    def record_approval(self, run_id: str, decision: str, approver: str, comment: str | None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO approvals (run_id, decision, approver, comment, created_at) VALUES (?,?,?,?,?)",
                (run_id, decision, approver, comment, time.time()),
            )

    def approvals(self, run_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM approvals WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        return [dict(r) for r in rows]
