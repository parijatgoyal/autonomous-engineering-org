# Autonomous Enterprise Engineering Organisation

A working vertical slice of a multi-agent software delivery workflow. A requirement goes in, specialised agents plan, design, implement, test and review it on an isolated git branch, and the change reaches `main` only after a named human approves it.

**Stack:** Python 3.11+, FastAPI, Pydantic v2, SQLite, Anthropic SDK (Claude), pytest, ruff, bandit, git.

```
requester ─► analyst ─► [plan review] ─► architect ─► developer ─► QA ─► security ─► release ─► [sign-offs] ─► tech lead ─► merge + deploy
               (person)                       ▲          │  ▲         │         │          (role-based)   (four-eyes)
                                              └negotiate─┘  └─feedback┴─────────┘ (bounded loop)
```

## Who uses it and how

Everyone signs in. What you see and can do depends on your role, and the home screen shows **what's waiting on you**.

| Person | Demo login | What they do |
|---|---|---|
| **Requester** (product manager) | `asha` / `asha-demo` | Submits a requirement (typed or a `.md` file) against a registered repo. Confirms the acceptance criteria the analyst drafted and answers its open questions, or asks for a revised plan. Follows progress and can withdraw their own request. |
| **Engineer** | `dev` / `dev-demo` | Handles escalations: settles architect/developer conflicts, sends stuck work back with guidance, resumes paused runs. Downloads the patch or inspects the agent branch locally. Signs off where the review policy names engineers. |
| **Security reviewer** | `sam` / `sam-demo` | Sees the remaining findings inline and either signs off or sends the work back. High and critical findings never reach them: they automatically send the work back to the developer agent. |
| **Tech lead** (approver) | `priya` / `priya-demo` | Gives the final approval to merge and deploy once every sign-off is in. **Can never approve their own request** (four-eyes rule). Can reject or send back. |
| **Admin** | `admin` / `admin-demo` | Operates the platform: sees every run, the audit trail and spend; can resume or close runs. Cannot approve releases. |

Agents can never approve anything. Only signed-in people can.

### Role-based review pipeline

The human sign-offs needed before the tech lead can approve are policy, not code. Copy the defaults into `review_policy.json` at the project root and enable or add gates:

```json
{"release_signoffs": [
  {"id": "security", "role": "security", "label": "Security sign-off", "when": "has_findings", "enabled": true},
  {"id": "service_owner", "role": "engineer", "label": "Pricing owner review", "when": "touches:services/pricing/*", "enabled": true},
  {"id": "extra_engineering_review", "role": "engineer", "label": "Engineering review", "when": "iterations>2", "enabled": false}
]}
```

Conditions: `always`, `has_findings`, `touches:<glob>` (CODEOWNERS-style), `iterations>N`. Gates are collected in order, each by a user with that role who isn't the requester, and the tech lead approval always comes last. See [aeeo/policy.py](aeeo/policy.py).

### Slack notifications

Whenever a run changes state, the person who owns the next step gets a Slack message with an **Open run** button:

| Moment | Who is notified |
|---|---|
| Analyst drafted the plan (plan review on) | the requester |
| Checks passed and a sign-off is needed | users with that gate's role (e.g. security), never the requester |
| All sign-offs in | tech leads (not the requester) |
| Architect and developer deadlock | engineers and tech leads |
| Agents stuck (no progress, budget used up) | engineers |
| Run paused (model provider down) | engineers |
| Shipped or closed | the requester |

Nobody else is pinged. Each message is sent once per step (deduplicated), so a re-check never spams anyone.

```bash
export SLACK_BOT_TOKEN=xoxb-...            # direct messages (bot needs chat:write)
export AEEO_SLACK_IDS="asha=U012AB3CD,sam=U045EF6GH,priya=U078IJ9KL"   # username -> Slack member ID
export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...           # optional team-channel fallback, @-mentions people
export AEEO_PUBLIC_URL=https://delivery.internal.example                # where "Open run" links point
```

Slack IDs can also go in `"slack_id"` in `.aeeo/users.json`. Without a token or webhook, notifications are still recorded and shown on the **Notifications** page (bell icon), just not sent. Every message is written to an outbox table first and delivered in the background; a Slack failure is recorded on the notification and never blocks the run. See [aeeo/notifications.py](aeeo/notifications.py).

## Quick start

```bash
cd aeeo
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# Dashboard: sign in as any demo user (see table above)
.venv/bin/python -m aeeo.cli serve            # http://127.0.0.1:8000

# Or CLI: minimum demo (one failed check, then the fix, then sign-off and approval)
.venv/bin/python -m aeeo.cli run --by asha --faults buggy_first_impl
.venv/bin/python -m aeeo.cli signoff <run_id> --gate security --by sam
.venv/bin/python -m aeeo.cli approve <run_id> --by priya
.venv/bin/python -m aeeo.cli users            # list demo accounts

# Every failure scenario in one table
.venv/bin/python -m aeeo.cli demo

# Tests (32: workflow scenarios, roles and gates, Slack notifications, safety guards, API journeys)
.venv/bin/python -m pytest
```

It runs offline by default with a scripted mock model. To use Claude:

```bash
export ANTHROPIC_API_KEY=...        # or `ant auth login`
export AEEO_PROVIDER=anthropic      # model defaults to claude-opus-5-5 (AEEO_MODEL, AEEO_EFFORT)
.venv/bin/python -m aeeo.cli run --by asha --requirement requirements/discount_codes.md
```

## What happens in a run

| Stage | Who | Output | Deterministic gate |
|---|---|---|---|
| analysis | Analyst agent | Given/When/Then acceptance criteria, tasks, assumptions, open questions | schema + ≥3 unique ACs |
| architecture | static inspector, then Architect agent | ranked impacted components (AST index, reverse dependencies), design, files to modify/create | listed files must exist / must not exist |
| development | Developer agent | full-file edits, committed on `agent/<run_id>` | **scope guard**, **repeat-change guard**, path sanitising |
| qa | QA agent, then real tool runs | acceptance tests mapped to every AC; `pytest` + `ruff` | all tests pass, lint clean, every AC covered |
| security | policy scanner + bandit, then Security agent | merged findings and verdict | any high/critical finding blocks; the agent cannot overrule the scanner |
| release | rubric, then Release agent | review summary, test evidence, known risks, rollback plan | none: it only recommends |
| plan review (optional, on by default in the UI) | **requester** | confirm acceptance criteria, answer open questions, or ask for a revision | nothing is designed or built until confirmed |
| sign-offs | **reviewers by role** (policy) | sign off or send back | every required sign-off present |
| approval | **tech lead** | approve / reject / request changes | not the requester; all gates re-checked at approval |

Failed checks go back to the developer as concrete feedback (failing test names, assertion messages, findings with file:line), up to a 3-iteration budget.

## Failure and exception scenarios

Each one can be injected from the dashboard or with `--faults`, and each has a test in `tests/test_workflow.py`.

| Scenario (`--faults`) | What happens | How the workflow responds |
|---|---|---|
| `buggy_first_impl` | First implementation ignores case, so the AC3 test fails | `check_failed` → feedback to developer → iteration 2 passes |
| `security_issue` | Developer uses `eval()` | Policy scanner flags it **critical** → release blocked → developer replaces it |
| `security_persistent` | Developer keeps using `eval()` | Iteration budget runs out → `NEEDS_HUMAN`; approval is refused while the finding is open |
| `disagreement` | Developer objects to the architect's design | One bounded negotiation round → architect revises the design (kept in history) |
| `disagreement_deadlock` | Architect rejects the objection | `NEEDS_HUMAN` with both positions shown; a human chooses (`resolve`) |
| `malformed_output` | Analyst returns broken JSON | Validation error fed back in a repair prompt (max 2) → valid output; both attempts recorded |
| `provider_outage` | Model API returns 503 at the security stage | 3 retries with backoff → `PAUSED` at checkpoint → `resume` continues; finished steps are replayed from storage, not re-run |
| `scope_violation` | Developer also edits `services/notifications/email.py` | Whole changeset rejected before any write, violation logged, developer retries within scope |
| `stuck_developer` | Developer resubmits an identical failing change | Changeset hash matches a previous attempt → loop stopped → `NEEDS_HUMAN` |

Also handled: non-retryable provider errors and refusals (escalate), cost budget per run (`--max-cost`, escalates when exceeded), QA trying to write outside `tests/` or modify existing tests, path traversal (`../`, absolute paths, `.git/`).

## Design decisions

**The orchestrator decides, agents advise.** Agents return validated data. Every transition is decided by code using deterministic signals: test results, scanner output, scope checks. An agent saying "looks good" cannot move a run forward on its own.

**Where autonomy stops.** Agents may write code, but only on an isolated branch in a copy of the repo. Merging and deploying always need a named human, and a human is called in for anything the agents can't settle: design deadlock, an exhausted iteration budget, no progress, persistent malformed output, or the cost budget. Critical security findings can't be approved at all; the only options are sending the work back or rejecting it.

**Separation of duties.** The developer can't edit tests written by QA (otherwise it could weaken them to pass). QA can't modify existing tests or write outside `tests/`. The security agent can add findings but can't dismiss scanner findings.

**Idempotency and resumability.** Every agent call has a step key (`development:2:d0`). A completed step's output is stored and replayed on resume, so an outage never causes duplicate commits or repeated work. Identical file writes are no-ops, and identical changesets are detected by hash.

**Observability.** SQLite holds the run checkpoint, every agent attempt (status, latency, input/output tokens, cost, error), an append-only event log and every human decision. The dashboard shows a timeline, plan, design, inspector ranking, diff, check history, the feedback loop, per-agent usage and the rubric.

**Evaluation.** Every run is scored against a 9-point rubric: testable ACs, every AC verified by a passing test, all tests pass, **no regressions against the baseline** (the original suite is run before any change), tests added, lint clean, no open high/critical findings, changes confined to the design, and within the iteration budget.

## Project layout

```
aeeo/
  orchestrator.py   state machine, gates, escalation, approval, "who acts next"
  users.py          roles, permission matrix, four-eyes rule, sessions
  policy.py         role-based review pipeline (configurable sign-off gates)
  agents/base.py    validation, repair loop, retries, idempotent replay, usage recording
  agents/roles.py   7 agents with explicit responsibilities and I/O contracts
  models.py         Pydantic contracts for every agent output
  llm.py            Claude provider (structured JSON output, server-side refusal fallback)
  mock_provider.py  deterministic offline model + fault injection
  workspace.py      repo copy, git branch isolation, scope guard, allowlisted sandboxed commands
  inspector.py      AST index + relevance ranking + reverse dependency graph
  checks.py         pytest (JUnit parsing), ruff, policy scanner, bandit
  rubric.py         rubric + baseline comparison
  store.py          SQLite persistence
  api.py, cli.py    authenticated REST API and CLI (same rules for both)
  notifications.py  Slack notifications (DM / webhook / outbox), dedupe, background delivery
  static/index.html GitHub-style UI: PR-like run page (conversation, merge box, reviewers), files changed
                    (unified/split diff, file tree, syntax colouring, viewed), checks, plan, audit, notifications
sample_repo/        4-service demo codebase (pricing, orders, inventory, notifications)
requirements/       sample requirement
tests/              workflow scenarios, unit tests for guards, API test
```

## Assumptions, mocks and limitations

- **The mock model is scripted** for the bundled requirement and sample repo. It shows orchestration, safety and failure handling deterministically. It does not show that a model produces good code; use `AEEO_PROVIDER=anthropic` for that. Mock token counts are estimated (chars / 4) and priced at Claude Opus 5.5 rates so the cost tracking path is exercised.
- **The Claude provider has not been run against the live API in this build** (no key was available). It uses the documented SDK calls and is protected by the same validation and repair loop, and it falls back to prompt-only JSON if the strict schema is rejected.
- **Deployment is simulated**: approval merges the branch in the run's workspace repo and writes `deployment.json`. There is no real CI/CD or environment.
- **The sandbox is process-level**, not a container: allowlisted tools (`pytest`, `ruff`, `bandit`) only, timeouts, secrets scrubbed from the environment, and path confinement for writes. Generated tests still execute as the current user. For production, run tool execution in a container or VM with no network access.
- **Single process**: runs execute in background threads with SQLite. A production version would use a durable queue/workflow engine (e.g. Temporal) and Postgres.
- **Demo identity**: users and passwords are seeded into `.aeeo/users.json` (PBKDF2-hashed, sessions in SQLite, HttpOnly cookie). A real deployment would use SSO/OIDC and pull roles and code owners from the directory.
- **Slack delivery has only been tested against a fake Slack endpoint** in the test suite; it uses the documented `chat.postMessage` and incoming-webhook formats. No email channel yet.
- **Repositories are a fixed registry** (`shopkit`); the UI can't point the agents at arbitrary server paths. The CLI still accepts a local path for operators.
- The developer returns whole files, which suits small modules but would be replaced by patch-based edits for large files.
- Only Python repositories are inspected and tested.
