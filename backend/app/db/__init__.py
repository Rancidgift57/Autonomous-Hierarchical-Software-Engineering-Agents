"""SQLAlchemy 2.x persistence layer for AHSEA (Phase 17).

Architecture (mirrors the API layer added in Phase 16)::

    API  ->  Service  ->  Repository  ->  Database

* ``app.db.config``       -- environment-driven `DatabaseSettings` (Postgres
  in production, SQLite for local development).
* ``app.db.base``         -- the shared declarative `Base` + naming
  convention used by every ORM model (and by Alembic autogeneration).
* ``app.db.models``       -- one ORM model per table listed in the Phase 17
  spec: projects, agents, tasks, artifacts, contracts, events, errors,
  test_results, deployment_runs, architecture_decisions, repair_attempts,
  llm_requests.
* ``app.db.session``      -- async engine/session management + the
  `get_db` FastAPI dependency.
* ``app.db.repositories``  -- one repository per table. Repositories are the
  *only* code in AHSEA allowed to construct a SQLAlchemy `select`/`insert`/
  `delete` statement -- services and routers never touch the ORM directly.
* ``app.db.converters``   -- pure functions translating between the
  Pydantic domain models in `app.state.models` (and the small schema
  modules in `app.llm`, `app.self_healing`, `app.deployment`) and the ORM
  rows in `app.db.models`.
* ``app.db.persistence_service`` -- `PersistenceService`, the Service layer
  a caller (typically `app.api.services.project_service.ProjectService`)
  depends on. It never leaks a `Session` or an ORM row to its caller --
  only plain domain/Pydantic objects.

Sensitive data policy: LLM prompt/response text and secrets are never
persisted by default. `DatabaseSettings.persist_llm_prompts` must be
explicitly set (`AHSEA_PERSIST_LLM_PROMPTS=true`) for
`PersistenceService.record_llm_request` to store the optional prompt
excerpt it is given -- and even then, only what the caller explicitly
passes in, never full conversation history.
"""

from __future__ import annotations
