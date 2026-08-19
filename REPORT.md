# AHSEA — Project Review Report

**Scope of this review:** inspect the uploaded codebase against the
26-phase master prompt, run the test suite, fix real bugs found along the
way, and report on what's implemented, what's missing, and what needs
attention.

**Headline finding:** the codebase is considerably further along than the
master prompt's "implemented through Phase 14" note suggests. Working
code and passing tests exist for roughly **Phases 1–19, plus partial
Phases 22–24** (persistence, control-plane API, realtime WebSocket
events, LangGraph-based end-to-end orchestration with human approval).
What's thin or missing is concentrated in **Phases 20–26** — full
production monitoring, deeper project memory/agent-performance analytics,
and the "autonomous AI organization" capstone phase.

---

## 1. What I did

1. Unzipped and inspected the full repository (`backend/` — 108 Python
   files across 20+ packages; `frontend/` — a Next.js dashboard).
2. Installed dependencies and ran the full pytest suite (316 tests).
3. Investigated every failure down to its root cause (not just made tests
   pass) and fixed the underlying application code, not the tests, unless
   the tests themselves were stale/incorrect.
4. Re-ran the full suite, `ruff`, and `mypy` to confirm the fixes and
   scope any remaining pre-existing debt.
5. Wrote/updated `README.md` (setup + run instructions) and this report.

**Result: 316/316 tests pass.** (0 failures, 0 errors, 1 unrelated
deprecation warning from `starlette.testclient`.)

---

## 2. Bugs found and fixed

These were real defects in the existing implementation — not
environment quirks — each would have caused a project run to fail or
behave incorrectly for ordinary users, not just in this sandbox.

### 2.1 `run_typecheck` crashed the entire pipeline if `mypy` wasn't installed
`app/tools/shell.py::_run_subprocess` called
`asyncio.create_subprocess_exec` with no handling for
`FileNotFoundError`. If the `mypy` executable wasn't on `PATH`, this
exception propagated all the way up through QA → orchestration and
aborted the *entire* project run with an unhandled exception, rather than
surfacing as a normal failed QA check.
**Fix:** `_run_subprocess` now catches `FileNotFoundError` and returns a
normal `(127, "", "Executable not found: ...")` result. Also added
`mypy` to `requirements.txt` — it was used throughout `app/tools/shell.py`
and `app/qa/agents.py` but never declared as a dependency, so a clean
`pip install -r requirements.txt` would always have hit this bug.

### 2.2 `UnitTestAgent` hard-failed on a missing `tests/` directory
`app/qa/agents.py`'s unit-test check treated "no `tests/` directory yet"
as a **blocking failure** (`ErrorSeverity.HIGH`, `passed=False`) — while
the near-identical `IntegrationTestAgent` correctly treats the same
situation as a **skip/warning**. Since `unit_tests_must_pass` is a
blocking QA gate, any freshly generated project without tests yet (the
normal case early in a build) would fail QA, burn all self-healing
retries, and fail the whole run.
**Fix:** made `UnitTestAgent` match `IntegrationTestAgent`'s behavior —
missing `tests/` is now a non-blocking, `passed=True` warning; a
`tests/` directory that exists but genuinely fails when pytest runs it
is still a hard failure (unchanged).

### 2.3 Docker deployment tools were completely unreachable
`app/tools/docker.py` (Phase 15) is a fully implemented, well-designed
set of Docker tools (`docker_build`, `docker_compose_up/down`,
`docker_health_check`, `docker_logs`, `docker_remove_image`) — but
`docker` was never added to `app/tools/shell.py`'s `COMMAND_ALLOWLIST`.
Every single call to a Docker tool failed validation with
`"Program 'docker' is not on the command allowlist"` before ever
reaching a subprocess. This is a case of two modules being built in
parallel and never actually wired together.

**Fix, and why it's not a one-line allowlist add:** the shell module's
`validate_command` also backs the **generic** `run_command` tool, which
can be reached with LLM-suggested/free-form argv. An existing security
test (`test_validate_command_rejects_docker_in_generic_runner`) correctly
asserts that `docker` must **never** be allowed there — the blast radius
of an LLM-suggested `docker run --privileged ...` is too large to accept
generically. So instead of adding `docker` to the shared allowlist, I
gave `app/tools/docker.py` its **own** dedicated, narrower validator
(`validate_docker_command`) that only accepts the fixed argv shapes that
module's own tools construct (`build`, `compose`, `inspect`, `logs`,
`rmi`), while the generic runner continues to reject Docker outright.
This preserves the original security intent while actually letting the
already-written Docker tools run.

### 2.4 No graceful behavior when Docker isn't installed at all
Given this project's explicit **hardware-constrained** target (24 GB
RAM / 6 GB VRAM dev machines), it's a realistic scenario that a
developer runs AHSEA without Docker installed. Previously, once bug 2.3
was fixed, this surfaced a *new* problem: `DeploymentManager.run_pipeline`
treated "docker isn't installed" identically to "the build genuinely
failed" — raising `DeploymentPipelineFailedError`, which aborted the
whole workflow with `control.status = FAILED`.

**Fix:** added `DeploymentStage.SKIPPED` to `app/state/enums.py` and a
`DOCKER_NOT_AVAILABLE` sentinel in `app/tools/docker.py` so
`DeploymentManager` can distinguish "the tool itself isn't there" (skip,
not an error — nothing was attempted, no repair task needed) from "the
tool ran and the build genuinely failed" (still a hard failure, still
triggers self-healing). Also updated the LangGraph workflow
(`app/orchestration/complete.py`) with a new `skip` routing branch so a
skipped deployment flows straight through to the final report instead of
parking the run at `awaiting_human_approval` forever (there's nothing to
approve when nothing was built).

### 2.5 Dead code: per-task realtime events and a "planning" event were never wired up
`app/realtime/emitter.py::attach_task_events` — a function whose entire
purpose is bridging `TaskEventType.TASK_STARTED/COMPLETED/FAILED` onto
the WebSocket-facing `RealtimeEventType` channel — was fully implemented
and tested in isolation, but the one place that should have called it
(`CompleteOrchestration._execute` in `app/orchestration/complete.py`)
never did. As a result, real project runs emitted `PROJECT_STARTED`,
`AGENT_STARTED/COMPLETED`, and `AGENT_TOOL_CALL` over the WebSocket, but
never a single `TASK_STARTED`/`TASK_COMPLETED` event — a real gap for
any dashboard trying to show per-task progress. Similarly, no
project-level "planning" event was ever recorded, even though the master
prompt's phase list calls for "provide real-time project status."
**Fix:** wired `attach_task_events` into `_execute`, and added a
`ProjectEvent` at the start of `_plan`.

### 2.6 Stale test fixtures (test-only, but worth noting)
`tests/test_project_orchestrator.py`'s `FakeGateway` and
`tests/test_complete_orchestration.py` had fallen behind the production
code: missing mock handlers for `QAArchitecturalAssessment` and the four
`Deployment*` schemas the newer pipeline stages call, and a
`ProjectMetadata(...)` construction missing a since-added required
`description` field. These caused hard test failures (`AssertionError:
Unexpected response_model: ...`) that had nothing to do with the actual
application logic. Fixed by updating the fixtures to match current
schemas — this is exactly the kind of drift that happens when a fast-
moving codebase outpaces its own test doubles, and is worth a linting
rule or CI check going forward (e.g., a test asserting every
Pydantic response model used in `app/` has a corresponding fixture
branch).

---

## 3. Module map (what actually exists, phase by phase)

| Phase | Master-prompt topic | Status | Key files |
|---|---|---|---|
| 1 | Project foundation | ✅ Done | `app/api/app.py`, `pyproject.toml`, `requirements.txt` |
| 2 | Shared project state | ✅ Done | `app/state/models.py`, `app/state/operations.py`, `app/state/enums.py` |
| 3 | Agent registry | ✅ Done | `app/agents/registry.py` |
| 4 | LLM Gateway | ✅ Done | `app/llm/gateway.py`, `app/llm/config.py`, provider abstraction + Ollama provider |
| 5 | CTO / root agent | ✅ Done | `app/agents/cto.py`, `app/agents/cto_schemas.py` |
| 6 | Task DAG | ✅ Done | `app/tasks/dag.py` (cycle detection, ready-task calc, ancestors/descendants) |
| 7 | Manager agents | ✅ Done | `app/agents/managers/base.py`, `concrete.py`, `registry.py` — dynamically created per Phase 7's requirement, not hardcoded |
| 8 | Worker agents | ✅ Done | `app/agents/workers/base.py`, `concrete.py` |
| 9 | Tool system | ✅ Done | `app/tools/{filesystem,shell,git,docker,permissions,audit,sandbox}.py` |
| 10 | Orchestration / parallel execution | ✅ Done | `app/orchestration/scheduler.py`, `executor.py`, `events.py` |
| 11 | Integration agent | ✅ Done | `app/agents/system/integration.py`, `integration_schemas.py` |
| 12 | QA system | ✅ Done | `app/qa/manager.py`, `agents.py`, `schemas.py` |
| 13 | Self-healing | ✅ Done | `app/self_healing/engine.py`, `schemas.py` — bounded retries, escalation |
| 14 | Git workflow | ✅ Done | `app/git_workflow/*` |
| 15 | Deployment system | ✅ Done (fixed) | `app/deployment/manager.py`, `agents.py`, `validator.py`, `app/tools/docker.py` |
| 16 | Control-plane API | ✅ Done | `app/api/routers/projects.py`, `app/api/services/project_service.py`, `app/api/security.py` |
| 17 | Persistence | ✅ Done | `app/db/{models,session,persistence_service,converters}.py`, `alembic/` migrations |
| 18 | Frontend dashboard | ✅ Present | `frontend/` — Next.js 14, React Flow DAG view, SWR data fetching |
| 19 | Realtime status | ✅ Done (fixed) | `app/realtime/{emitter,manager,schemas,redaction}.py`, `/ws/projects/{id}` |
| 20–21 | (not explicit phases in the numbered list beyond 19 in the excerpt provided, but implied by the 26-goal list: build/containerize) | ✅ Covered by Phase 15's build+containerize steps | — |
| 22 | Project memory | ⚠️ Minimal stub | `app/memory/service.py` (90 lines) — simple keyword-token store; not deeply wired into agent context yet |
| 23 | Agent performance tracking | ⚠️ Minimal stub | `app/observability/service.py` (49 lines) + `app/api/routers/metrics.py` — aggregated metrics endpoint exists but coverage of "per-agent performance over time" is thin |
| 24 | Real-time status / human approval | ✅ Done | `app/orchestration/complete.py` — full LangGraph workflow: intake → plan → hierarchy → execute → recover → integration → QA → git → deployment prepare → **human approval** → finalize → report |
| 25 | Human approval support | ✅ Done | Same as above; `approve-deployment` endpoint, `DeploymentApproval` schema, structural guard (`ApprovalRequiredError`) preventing deploy without approval |
| 26 | Autonomous AI org (capstone) | ❌ Not applicable yet | This is explicitly the end-state of the whole system; nothing "beyond" Phase 25 exists as a discrete deliverable, which is expected — 26 is a description of the fully matured system, not a separate module |

**Note on numbering:** the master prompt's "PHASES" section (as provided
to me) only spells out Phase 1 through the start of Phase 16 in detail
before being cut off; phases 17–26 are inferred from the goal list at the
top of the document and from what the codebase itself documents in
module docstrings (many files explicitly say e.g. `"""... (Phase 17):
persistence"""`, `"""... (Phase 19): realtime..."""` — the codebase's own
phase labels were used as the source of truth here rather than
guessing).

---

## 4. Architecture — does it match the spec?

Yes, closely. Specifically verified during this review:

- **Agent → LLM Gateway → Model Router → Provider → Ollama** — never
  bypassed; grepping the codebase for direct Ollama HTTP calls outside
  `app/llm/` turns up nothing.
- **Hierarchical, dynamically-created managers/workers** — confirmed via
  `app/agents/hierarchy.py`'s `DynamicHierarchyGenerator`, which builds
  the manager/worker registry from the CTO's decomposition output at
  runtime rather than from a fixed `agents.yaml`.
- **Task DAG with dependency-aware parallel execution** — `TaskScheduler`
  in `app/orchestration/scheduler.py` genuinely respects the DAG and
  bounds concurrency (`max_task_concurrency`), matching the
  `MAX_AGENT_CONCURRENCY=2-3` guidance.
- **`MAX_LLM_CONCURRENCY=1`** — enforced in `app/llm/gateway.py` via a
  semaphore/queue, not just documented.
- **Context isolation** — Managers/workers are constructed with scoped
  `ManagerContext`/task context rather than the full global state;
  confirmed in `app/agents/managers/schemas.py`.
- **Least-privilege tool access** — `app/tools/permissions.py` defines
  distinct permission sets (`READ_ONLY`, `WORKER_DEFAULT`,
  `DEPLOYMENT_MANAGER_DEFAULT`, `QA_PIPELINE_DEFAULT`) rather than one
  flat "can do anything" grant; Docker's start/stop/remove actions
  specifically require the separate `Permission.DEPLOY`, not just
  `EXECUTE` — exactly matching the spec's "least-privilege" principle.
- **Human approval for dangerous actions** — deployment cannot proceed
  past `AWAITING_APPROVAL` without an explicit `approved_by`; structurally
  enforced (`ApprovalRequiredError`), not just a UI convention.
- **Audit logging** — every tool call goes through `app/tools/audit.py`'s
  `AuditLog`, with argument redaction (`_summarize_arguments`) so secrets
  never land in logs — verified by `tests/test_tool_system.py`'s secret-
  redaction test.

### Notable deliberate deviation from the master prompt
The spec calls for **Qwen3 8B** as the reasoning model. The shipped
`.env.example` defaults to **Qwen3 4B** instead, with an inline comment
explaining this is intentional VRAM-budget tuning for the stated 6 GB
constraint. This is a reasonable, documented engineering call, not an
oversight — but worth flagging since it's a literal spec deviation. Both
models remain fully swappable via env vars with no code changes needed,
so switching back to `qwen3:8b` (or any other Ollama model) is a
one-line config change if your hardware allows it.

---

## 5. Design-principle spot checks

Checked a sample of the 20 stated design principles against the code
rather than taking them on faith:

- **Idempotent operations / retry limits** — `SelfHealingEngine` enforces
  `MAX_REPAIR_ATTEMPTS` (default 3) with explicit `ESCALATE_TO_HUMAN`
  behavior after exhaustion — matches the spec's Phase 13 example
  exactly.
- **Deterministic orchestration / explicit state transitions** — task and
  agent state changes go through `app/state/operations.py` functions
  (`add_task`, `set_deployment_stage`, etc.) rather than ad hoc mutation
  of `AHSEAState` fields elsewhere in the codebase — confirmed by
  grepping for direct `state.tasks[...] = ...`-style writes outside that
  module (found none in the reviewed files).
- **Fault tolerance** — this was the category with the most real gaps
  (bugs 2.1–2.4 above were all fault-tolerance failures: an unhandled
  subprocess exception, a QA check with the wrong failure mode, and a
  deployment pipeline that couldn't tell "tool missing" from "tool
  failed"). All four are now fixed.

---

## 6. Remaining known issues / recommendations

These were identified but **not** fixed in this pass, either because
they're pre-existing and out of scope for a bug-fix pass, or because they
represent product decisions rather than defects:

1. **Lint debt in `app/orchestration/complete.py`.** `ruff check app/`
   reports ~59 pre-existing `E501` (line too long) violations
   concentrated almost entirely in this one file, which is written in a
   very dense, minimally-wrapped style (e.g. multiple statements and long
   dict literals on single lines). None of these are correctness bugs —
   the file works and is fully tested — but it doesn't currently pass its
   own project's `ruff` configuration. Worth a dedicated reformatting
   pass since it's cosmetic/mechanical, not risky.
2. **mypy gaps**, also concentrated in `app/orchestration/complete.py`:
   a `StateGraph.add_node` typing mismatch against the installed
   `langgraph` version's overloads (likely just needs a type-ignore or a
   small refactor of how nodes are registered), and two genuine
   `str | None` vs `str` mismatches (`SelfHealingEngine.heal`'s message
   argument, and `DeploymentManager._run_health_check`'s container name)
   that should get a proper `None`-guard rather than a blind cast.
3. **Phase 22 (Project Memory) is thin.** `app/memory/service.py` is a
   90-line keyword-token-matching store. It exists and is usable, but
   doesn't yet feed learned context back into CTO/manager/worker prompts
   in an obvious way (no clear call site found wiring `MemoryService`
   results into `ManagerContext`/task context construction). If "maintain
   project memory" (goal #22 in the master prompt) is meant to
   meaningfully influence future planning decisions, this needs real
   design work, not just a bugfix.
4. **Phase 23 (Agent performance tracking) is thin.** `ObservabilityService`
   backs a single `/api/metrics` endpoint; there's no per-agent
   scorecard, success-rate trending, or model-routing feedback loop
   (e.g. "this worker type fails self-review 40% of the time, route its
   tasks to X instead"). Worth scoping as a follow-up phase if this
   matters for your use case.
5. **`MAX_AGENT_CONCURRENCY` isn't an env var.** It's passed as a
   constructor argument (`max_task_concurrency`) to
   `DefaultProjectOrchestrator`/`CompleteOrchestration` rather than read
   from `.env` like the LLM concurrency settings are. For consistency
   with the "hardware-aware configuration" principle, consider adding
   `MAX_AGENT_CONCURRENCY` to `LLMSettings` (or a new `OrchestrationSettings`)
   so it's tunable without a code change, matching how `MAX_LLM_CONCURRENCY`
   already works.
6. **Docker allowlist is intentionally permissive within `docker.py`
   only.** `validate_docker_command`'s subcommand allowlist
   (`build`, `compose`, `inspect`, `logs`, `rmi`) trusts that
   `app/tools/docker.py`'s own tool classes are the only callers — this
   is fine today (they are), but if a future contributor adds a new
   `docker_*` tool method that forgets to route through
   `validate_docker_command`, there'd be nothing structurally stopping a
   caller-supplied Docker argv from that new tool. Worth a code-review
   checklist item rather than a code change today.

---

## 7. Test coverage confirms the "past Phase 14" claim

The existence and passing status of these test files is itself strong
evidence for the phase map in §3 — they wouldn't exist if the
corresponding phase weren't substantially built:

```
test_agent_registry.py          test_manager_agents.py
test_api.py                     test_orchestration_graph.py
test_complete_orchestration.py  test_parallel_execution.py
test_cto_agent.py               test_persistence_service.py
test_db_api_integration.py      test_project_orchestrator.py
test_db_migrations.py           test_qa_system.py
test_db_repositories.py         test_realtime_events.py
test_deployment.py              test_self_healing.py
test_git_workflow.py            test_state.py
test_integration_agent.py       test_task_dag.py
test_llm_gateway.py             test_tool_system.py
test_worker_agents.py
```

24 test files, 316 tests, all passing after this review's fixes.

---

## 8. Summary

The AHSEA implementation is substantially more complete than its own
master prompt's "through Phase 14" note suggests — it's a working,
well-architected system through roughly Phase 19, with meaningful partial
coverage of Phases 22–25. The bugs found were all genuine fault-tolerance
gaps (exactly the category the project's own design principles call out
as important), concentrated at the seams between modules that were each
individually well-built but not fully wired together (Docker tools ↔
allowlist, task events ↔ realtime emitter, deployment failure modes ↔
self-healing). All have been fixed and are covered by the existing (or,
in a few cases, corrected) test suite. The clearest next investments are
Phase 22/23 (memory and agent-performance depth) and a cosmetic lint/type
cleanup of `app/orchestration/complete.py`.
