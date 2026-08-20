# AHSEA — Autonomous Hierarchical Software Engineering Agent

AHSEA turns a natural-language project idea — *"Build a full-stack
e-commerce app with auth, cart, and payments"* — into an autonomously
managed software build. A **CTO agent** plans the project, a dynamically
generated hierarchy of **manager** and **worker** agents implements it,
a **QA pipeline** and **self-healing engine** keep the result honest, and
the finished project can be Git-committed, containerized, and deployed —
all running on local hardware through **Ollama**.

This revision has been through several rounds of real-world debugging
(Linux + Windows) and is the most reliable version of the project to
date. See [What's been fixed](#whats-been-fixed-in-this-revision) for the
full list.

---

## Table of contents

1. [Architecture at a glance](#architecture-at-a-glance)
2. [Prerequisites](#prerequisites)
3. [Install and start Ollama](#1-install-and-start-ollama-with-the-two-local-models)
4. [Backend setup](#2-backend-setup)
5. [Configuration reference](#3-configuration-reference-env)
6. [Running the test suite](#4-running-the-test-suite)
7. [Starting the backend](#5-starting-the-backend-api)
8. [Driving a project end to end](#6-driving-a-project-end-to-end)
9. [API reference](#7-api-reference)
10. [Frontend dashboard](#8-frontend-dashboard-optional)
11. [How a project run actually works](#9-how-a-project-run-actually-works)
12. [Docker / deployment behavior](#10-docker--deployment-behavior)
13. [Project memory & observability](#11-project-memory--observability-phases-22-23)
14. [Troubleshooting](#12-troubleshooting)
15. [What's been fixed in this revision](#whats-been-fixed-in-this-revision)
16. [Project layout](#project-layout)

---

## Architecture at a glance

```
                         CTO Agent
                    (plans architecture,
                   requirements, task DAG)
                             │
                    dynamic hierarchy
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        Team Manager   Team Manager   Team Manager
         (READ_ONLY      (e.g.           (e.g.
          tools)        Backend)       Frontend)
              │
        ┌─────┴─────┐
        ▼           ▼
     Worker      Worker
   (full R/W/X   (full R/W/X
     tools)        tools)
        │
        ▼
   write_file / read_file / edit_file / run_command /
   run_pytest / run_lint / run_typecheck / git_* / docker_*
        │
        ▼
   QA Pipeline (unit tests, lint, typecheck, contract
   validation, code review) → Self-Healing (diagnose +
   repair, up to MAX_REPAIR_ATTEMPTS) → Git commit →
   Docker build/deploy (skips cleanly if unavailable)
```

Every LLM call is routed through a single **LLM Gateway**, never called
directly by an agent — this is what makes model routing, timeouts,
retries, and observability tracing consistent across the whole system.
Every tool call (file I/O, shell commands, git, docker) is routed through
a **`WorkspaceSandbox`** that confines it to the project's own workspace
directory, and through a **permission system** (`READ_ONLY` /
`WORKER_DEFAULT` / `DEPLOY`) that determines which tools each agent kind
can actually call.

---

## Prerequisites

| Requirement | Version / notes |
|---|---|
| Python | 3.11 or 3.12 |
| Node.js | 18.18+ (only needed for the optional dashboard) |
| Ollama | Latest — https://ollama.com/download |
| RAM | 24 GB recommended |
| GPU VRAM | 6 GB recommended (CPU-only works, just slower) |
| Docker | Optional — see [Docker / deployment behavior](#10-docker--deployment-behavior) |

---

## 1. Install and start Ollama with the two local models

AHSEA routes every LLM call through its own gateway, never talking to
Ollama directly from an agent — but Ollama itself still needs to be
running with both models pulled.

```bash
ollama serve &                      # starts the Ollama server on :11434

ollama pull qwen3:4b                                  # reasoning/planning model
ollama pull qwen2.5-coder:7b-instruct-q4_K_M           # coding/implementation model
```

Verify it's up:

```bash
curl http://localhost:11434/api/tags
```

> **Windows note:** if `ollama serve` fails with `bind: Only one usage of
> each socket address is normally permitted`, Ollama is already running
> as a background service (this is normal — the Windows installer sets
> it up to auto-start). Just skip `ollama serve` and go straight to
> `curl`/`Invoke-RestMethod http://localhost:11434/api/tags` to confirm
> it's answering.

> **Model size note:** the master spec calls for Qwen3 **8B**; the
> shipped default uses Qwen3 **4B** for 6 GB VRAM headroom. Point
> `OLLAMA_REASONING_MODEL` at `qwen3:8b` in `.env` if your hardware
> allows it — nothing else needs to change.

---

## 2. Backend setup

```bash
cd AHSEA/backend

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1

pip install --upgrade pip
pip install -r requirements.txt

cp .env.example .env
```

### Database

Tables are auto-created on API startup for local dev (SQLite) — no
migration step needed. For Postgres or migration history:

```bash
alembic upgrade head
```

---

## 3. Configuration reference (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Where Ollama is listening |
| `OLLAMA_REASONING_MODEL` | `qwen3:4b` | Planning / architecture / decomposition / review / root-cause analysis |
| `OLLAMA_CODING_MODEL` | `qwen2.5-coder:7b-instruct-q4_K_M` | Code generation / tests / docs |
| `LLM_REQUEST_TIMEOUT` | `300` | Per-request timeout, seconds. **Raise this if generation on your hardware routinely exceeds it** — a timeout is a clean, handled failure (see [Troubleshooting](#12-troubleshooting)), not a bug, but a low value on slow hardware means more retries. |
| `MAX_LLM_CONCURRENCY` | `1` | Concurrent inference calls — keep at 1 on 6 GB VRAM |
| `MODEL_KEEP_ALIVE` | `0` | Unload model from VRAM immediately after each call. Set non-zero (e.g. `300`) if you have VRAM headroom — avoids a full model reload before every single call, which is often the biggest real-world speed factor. |
| `DATABASE_URL` | `sqlite+aiosqlite:///./ahsea.db` | SQLite by default; `postgresql+asyncpg://...` for production |
| `DATABASE_ECHO` | `false` | Log every SQL statement (debug only) |
| `AHSEA_PERSIST_LLM_PROMPTS` | `false` | Safety switch — prompt/response text is never persisted unless explicitly enabled |
| `AHSEA_REQUIRE_API_KEY` | `false` | Set `true` to require `X-API-Key` on every API call |
| `AHSEA_API_KEYS` | *(empty)* | Comma-separated valid keys, e.g. `devkey1,devkey2` |
| `MAX_REPAIR_ATTEMPTS` | `3` | How many times self-healing retries a failed task before escalating |

`MAX_AGENT_CONCURRENCY` is passed as a constructor argument
(`max_task_concurrency`), not an env var, today.

---

## 4. Running the test suite

```bash
python -m pytest -q
```

You should see all tests pass (370+ as of this revision, including
dedicated regression coverage for every Windows-specific bug found and
fixed — see below). Lint/type-checking:

```bash
ruff check app/
mypy app/
```

---

## 5. Start the backend API

```bash
uvicorn app.api.app:create_app --factory --reload --port 8000
```

- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

---

## 6. Driving a project end to end

### curl (macOS/Linux)

```bash
BASE=http://localhost:8000/api/projects

curl -s -X POST $BASE -H "Content-Type: application/json" -d '{
  "name": "Todo App",
  "description": "A simple todo list app",
  "idea_prompt": "Build a full-stack todo app with a FastAPI backend, a health check endpoint, and unit tests."
}' | tee /tmp/project.json

PROJECT_ID=$(python3 -c "import json;print(json.load(open('/tmp/project.json'))['project_id'])")

curl -s -X POST $BASE/$PROJECT_ID/run
curl -s $BASE/$PROJECT_ID/status
curl -s $BASE/$PROJECT_ID/agents
curl -s $BASE/$PROJECT_ID/tasks
curl -s $BASE/$PROJECT_ID/events
curl -s $BASE/$PROJECT_ID/qa
curl -s $BASE/$PROJECT_ID/artifacts
curl -s $BASE/$PROJECT_ID/deployment
```

### PowerShell (Windows)

```powershell
$BASE = "http://localhost:8000/api/projects"

$body = @{
    name = "Todo App"
    description = "A simple todo list app"
    idea_prompt = "Build a full-stack todo app with a FastAPI backend, a health check endpoint, and unit tests."
} | ConvertTo-Json

$project = Invoke-RestMethod -Uri $BASE -Method POST -ContentType "application/json" -Body $body
$PROJECT_ID = $project.project_id

Invoke-RestMethod -Uri "$BASE/$PROJECT_ID/run" -Method POST
Invoke-RestMethod -Uri "$BASE/$PROJECT_ID/status" -Method GET
Invoke-RestMethod -Uri "$BASE/$PROJECT_ID/events" -Method GET
Invoke-RestMethod -Uri "$BASE/$PROJECT_ID/deployment" -Method GET
```

### Live progress over WebSocket

```
ws://localhost:8000/ws/projects/{project_id}
```

```bash
wscat -c "ws://localhost:8000/ws/projects/$PROJECT_ID"
```

Each message is a JSON event: `project_started`, `agent_started`,
`agent_completed`, `agent_tool_call`, `qa_started`, `qa_failed`, etc.
Pass `?after=<event_id>` to resume from a specific event after a
reconnect (as seen in the logs: `?after=rtevt_...`).

### Other run controls

```bash
curl -s -X POST $BASE/$PROJECT_ID/pause
curl -s -X POST $BASE/$PROJECT_ID/resume
curl -s -X POST $BASE/$PROJECT_ID/cancel
```

### If deployment reaches "awaiting approval"

Only happens if Docker is actually installed **and running** on your
host, and the build succeeded:

```bash
curl -s -X POST $BASE/$PROJECT_ID/approve-deployment \
  -H "Content-Type: application/json" \
  -d '{"approved_by": "you@example.com"}'
```

### Authentication

Disabled by default. To require it:

```bash
# in .env
AHSEA_REQUIRE_API_KEY=true
AHSEA_API_KEYS=devkey1,devkey2
```

Then pass `X-API-Key: devkey1` on every request.

---

## 7. API reference

All endpoints are under `/api/projects`.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/` | Create a project from an idea prompt |
| `GET` | `/` | List all projects |
| `GET` | `/{project_id}` | Project detail |
| `POST` | `/{project_id}/run` | Start the run (planning → execution → QA → git → deployment) |
| `POST` | `/{project_id}/pause` | Pause between task dispatches |
| `POST` | `/{project_id}/resume` | Resume a paused run |
| `POST` | `/{project_id}/cancel` | Cancel the run |
| `GET` | `/{project_id}/status` | Current status + task counts by state |
| `GET` | `/{project_id}/agents` | The generated agent hierarchy (CTO/managers/workers), including each agent's `allowed_tools` |
| `GET` | `/{project_id}/tasks` | Every task, including `assigned_agent_id` |
| `GET` | `/{project_id}/artifacts` | Files produced |
| `GET` | `/{project_id}/events` | Full event log |
| `GET` | `/{project_id}/qa` | QA pipeline reports |
| `GET` | `/{project_id}/deployment` | Deployment state |
| `POST` | `/{project_id}/approve-deployment` | Approve a build awaiting approval |
| `GET` | `/ws/projects/{project_id}` | WebSocket, live event stream |

Metrics endpoints (Phase 22/23, see [below](#11-project-memory--observability-phases-22-23)):

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/metrics?project_id=...` | Aggregated event metrics |
| `GET` | `/api/metrics/agents?project_id=...` | Per-agent success rate, avg duration, trend |
| `GET` | `/api/metrics/task-types?project_id=...` | Success rate by (task_type, model) |

---

## 8. Frontend dashboard (optional)

```bash
cd AHSEA/frontend
npm install
echo "NEXT_PUBLIC_API_BASE_URL=http://localhost:8000" > .env.local
npm run dev
```

Open http://localhost:3000 for the project list, project detail view, and
a DAG visualization of tasks.

---

## 9. How a project run actually works

1. **`project_intake`** — the project and its idea prompt are recorded.
2. **CTO planning** — three LLM calls (routed to the reasoning model):
   requirements → architecture → task decomposition. Produces a
   validated task DAG.
3. **Dynamic hierarchy** — a manager is created per team, workers per
   role within each team. Managers get `READ_ONLY` tool access; workers
   get the full `WORKER_DEFAULT` set (`write_file`, `read_file`,
   `edit_file`, `run_command`, `run_pytest`, `run_lint`,
   `run_typecheck`, git/docker tools, etc.).
4. **Task execution** — each ready task is dispatched to its team's
   manager, which selects a worker and hands off implementation. The
   worker: plans → implements (writes files through the sandbox) →
   reviews → tests → reports back.
5. **QA pipeline** — real `run_pytest`/`run_lint`/`run_typecheck`
   subprocess calls, plus contract validation and an LLM code review,
   evaluated against a small set of gates (e.g. "no critical/high
   findings"). A missing test directory or an unavailable lint/typecheck
   tool is treated as a warning, not a hard failure — only genuine
   findings block the gate.
6. **Self-healing** — if a task or QA check fails, the self-healing
   engine diagnoses the root cause (informed by relevant prior project
   memory, see below), assigns a rework task, and retries up to
   `MAX_REPAIR_ATTEMPTS` times before escalating to a human-visible
   error.
7. **Git commit** — the finished workspace is committed.
8. **Deployment** — Dockerfile/compose files are generated and, if Docker
   is actually usable on the host, built and started for a health check
   + smoke test, then paused for approval. If Docker isn't usable for
   *any* reason, this stage **skips cleanly** rather than failing the run
   (see next section).

---

## 10. Docker / deployment behavior

Deployment only runs if `docker` is genuinely usable on the host. Two
distinct "not usable" cases are both treated as a **clean skip**, never a
run-failing error:

- **Docker isn't installed at all** — the `docker` command isn't found.
- **Docker is installed but the daemon isn't running** — this is by far
  the more common case on Windows, since Docker Desktop has to be
  launched manually (it isn't an always-on background service the way
  the Linux daemon is). `docker build` starts fine but can't reach the
  daemon and exits with a real (non-"not found") error.

If you *want* AHSEA to actually build and run containers for a project,
just make sure Docker Desktop (or your Docker daemon) is running before
you call `/run` — no other configuration is needed; the deployment stage
detects it automatically.

---

## 11. Project memory & observability (Phases 22–23)

- **Project memory**: the CTO's architecture decisions, each task's
  outcome, and self-healing's repair/failure history are stored per
  project and fed back into later prompts in the same run (and any
  future run against the same project) — so a manager delegating a task
  can see what a related earlier task decided, and self-healing's
  diagnosis step can see what was already tried for a similar failure.
- **Per-agent performance**: `GET /api/metrics/agents` reports, per
  `agent_id`: event count, success rate, average duration, which
  models/task types it used, and a trend (`improving` / `declining` /
  `stable` / `insufficient_data`) comparing recent activity against
  earlier in the project.
- **Per-(task_type, model) breakdown**: `GET /api/metrics/task-types`
  reports success rate and average duration for each task-type/model
  pairing actually used — the data you'd want if deciding whether to
  route a particular kind of task to a different model.

---

## 12. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `Connection refused` to `localhost:11434` | Ollama isn't running — `ollama serve` (or, on Windows, it's likely already running as a background service — see note in [step 1](#1-install-and-start-ollama-with-the-two-local-models)) |
| A run hangs for a long time on a single task | Expected on CPU-only or VRAM-constrained hardware. Not a bug: `LLM_REQUEST_TIMEOUT` will fire cleanly and the task will fail/retry rather than hang forever. Raise the timeout, or set `MODEL_KEEP_ALIVE` non-zero to avoid a full model reload before every call. |
| A project run fails with `docker build failed` or similar | Docker isn't usable on the host (not installed, or Desktop not running). Should now skip cleanly — start Docker Desktop first if you want deployment to actually run. |
| A run used to fail with `Event loop is closed` warnings or crash on `NotImplementedError` | Fixed — subprocess and filesystem tool calls now degrade to a clean failed result instead of an unhandled exception, on every platform including Windows. |
| `assigned_agent_id` shows `null` on a task | Fixed — now set to the specific worker agent that handled it. |
| An agent's `allowed_tools` shows `[]` in `/agents` | Fixed for workers/managers (CTO legitimately has none — it never touches a `ToolExecutor`). |
| A project run fails immediately with a Pydantic validation error | The LLM produced output that didn't match the expected schema — check `/api/projects/{id}/events`; self-healing retries up to `MAX_REPAIR_ATTEMPTS` before escalating. |
| `mypy`/`ruff` command not found | `pip install -r requirements.txt` (both are included) |
| Tests fail only on Windows with a path-separator assertion | Fixed — all path output is normalized to forward slashes regardless of host OS. |

---

## What's been fixed in this revision

This project went through multiple rounds of real bug fixes, verified
with dedicated regression tests (370+ tests passing):

**Correctness / wiring**
- Project memory (`MemoryService`) existed but nothing called it — now
  wired into CTO planning, task dispatch, and self-healing diagnosis.
- Per-agent performance tracking existed only as raw event data — added
  `agent_scorecards()`/`task_type_model_scorecards()` and two new API
  endpoints.
- `Task.assigned_agent_id` was declared but never written — now set to
  the actual worker that handled each task.
- Dynamically generated agents always reported `allowed_tools: []` in
  the API — now populated from the agent's real permission set.

**Windows / cross-platform robustness**
- Subprocess creation (`run_pytest`, `run_lint`, `run_typecheck`,
  `run_command`, git/docker tools) could raise an uncaught
  `NotImplementedError` on Windows (Selector event loop doesn't support
  subprocess), crashing the entire run — now degrades to a clean failed
  result, and the Proactor event loop policy is set proactively on
  Windows so subprocess tools work for real.
- File tool operations (`write_file`, `read_file`, `edit_file`,
  `delete_file`, `list_files`) had zero exception handling around raw
  `Path` calls — any platform-level failure (locked file, path-length
  limit, permissions) crashed the task uncaught. Now degrades cleanly.
- `WorkspaceSandbox` path output used OS-native separators (`\` on
  Windows), silently breaking exact-string comparisons against the
  always-forward-slash paths an LLM produces — normalized to
  `.as_posix()` everywhere.
- The sandbox's blocked-directory list (`.git`, `node_modules`, `.venv`)
  was checked case-sensitively — not a real boundary on
  case-insensitive filesystems (Windows/macOS). Now case-insensitive.
- Backslash-separated paths were silently mis-parsed on POSIX hosts
  (backslash isn't a separator there) — now normalized before resolving.
- A POSIX-rooted path (`/etc/passwd`) is not considered "absolute" by
  Python's `pathlib` on Windows, creating a validation gap — explicitly
  closed.
- Windows-reserved device names (`con.py`, `nul.txt`, `com1.md`, ...)
  used to raise a raw, uncaught `OSError` at write time — now rejected
  up front with a clear validation error.
- `StaticAnalysisAgent` (lint/typecheck QA checks) treated "the tool
  couldn't even start" identically to "the tool ran and found real
  issues" — both failed the QA gate and could escalate to failing the
  whole run over missing/unusable tooling. Now the former is a
  non-blocking warning, matching how a missing `tests/` directory was
  already handled.
- Deployment failed the entire run when Docker was installed but the
  daemon wasn't running (the common Windows case — Docker Desktop must
  be launched manually) — now detected and treated as a clean skip, the
  same as Docker not being installed at all.
- A leaked SQLAlchemy engine in the test fixtures caused
  `RuntimeError: Event loop is closed` warnings from `aiosqlite`
  background threads — fixed with a proper `engine.dispose()`.

---

## Project layout

```
AHSEA/
├── backend/
│   ├── app/
│   │   ├── agents/          CTO, managers, workers, dynamic hierarchy generation
│   │   ├── api/              FastAPI app, routers, services
│   │   ├── db/                SQLAlchemy models, session, converters
│   │   ├── deployment/         Dockerfile/compose generation, build/deploy pipeline
│   │   ├── git_workflow/        Git commit automation
│   │   ├── llm/                 Gateway, Ollama provider, task-type routing
│   │   ├── memory/               Project memory (Phase 22)
│   │   ├── observability/         Event tracing, agent scorecards (Phase 23)
│   │   ├── orchestration/          Project run orchestration, self-healing wiring
│   │   ├── qa/                      Unit test / static analysis / code review / contracts
│   │   ├── realtime/                 WebSocket event emitter
│   │   ├── self_healing/              Diagnose + repair engine
│   │   ├── state/                      Project/task/agent state models
│   │   └── tools/                       Sandbox, filesystem, shell, git, docker tools
│   ├── tests/                             370+ tests, one file per module/phase
│   ├── alembic/                            DB migrations
│   ├── .env.example                         All runtime configuration, documented inline
│   └── requirements.txt
└── frontend/                                  Next.js dashboard (optional)
    ├── app/                                     Project list / detail / DAG view
    └── components/
```
---

## Contact

- **Email:** nnair7598@gmail.com
- **LinkedIn:** [linkedin.com/in/nikhil-nair-809248286](https://www.linkedin.com/in/nikhil-nair-809248286)

<div align="center">

*From Idea to Production — Autonomous AI Agents Engineering Software Together.*

**Thank you** 

</div>
