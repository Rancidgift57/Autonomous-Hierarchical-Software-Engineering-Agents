"""Concrete coding worker agents (Phase 8).

Each is a thin `BaseWorkerAgent` subclass: `worker_type` matches the
identifiers used in `config/agents.yaml` / `Task.worker_type`, plus a
role description that flavors the prompts built by the base class. None of
them override the READ->PLAN->IMPLEMENT->TEST->SELF REVIEW->SUBMIT
workflow -- behavioral differences come entirely from the charter text and
the `WorkerScope` each is constructed with (e.g. an `APIWorker` scoped to
`backend/app/api/`).
"""

from __future__ import annotations

from app.agents.workers.base import BaseWorkerAgent


class APIWorker(BaseWorkerAgent):
    worker_type = "api_worker"
    capabilities = ["python", "fastapi"]
    role_description = "Implements FastAPI routes, request/response models, and API contracts."


class AuthWorker(BaseWorkerAgent):
    worker_type = "auth_worker"
    capabilities = ["python", "security"]
    role_description = "Implements authentication, authorization, and session handling."


class ServiceWorker(BaseWorkerAgent):
    worker_type = "service_worker"
    capabilities = ["python"]
    role_description = "Implements core business/service-layer logic."


class UIWorker(BaseWorkerAgent):
    worker_type = "ui_worker"
    capabilities = ["typescript", "nextjs", "tailwind"]
    role_description = "Implements pages, routing, and layout using Next.js and Tailwind CSS."


class ComponentWorker(BaseWorkerAgent):
    worker_type = "component_worker"
    capabilities = ["typescript", "react"]
    role_description = "Implements reusable React components and shared UI primitives."


class SchemaWorker(BaseWorkerAgent):
    worker_type = "schema_worker"
    capabilities = ["python", "sqlalchemy", "postgresql"]
    role_description = "Designs and implements SQLAlchemy models and database schemas."


class MigrationWorker(BaseWorkerAgent):
    worker_type = "migration_worker"
    capabilities = ["python", "alembic"]
    role_description = "Writes and validates Alembic migrations."


class ModelWorker(BaseWorkerAgent):
    worker_type = "model_worker"
    capabilities = ["python", "llm_integration"]
    role_description = "Implements the LLM provider abstraction layer and Ollama integration."


class EvaluationWorker(BaseWorkerAgent):
    worker_type = "evaluation_worker"
    capabilities = ["python", "evaluation"]
    role_description = "Builds evaluation harnesses and quality checks for LLM outputs."


#: worker_type -> concrete class, used by managers (Phase 7) to instantiate
#: the right worker for a delegated task without hard-coding a big if/elif.
WORKER_CLASSES: dict[str, type[BaseWorkerAgent]] = {
    cls.worker_type: cls
    for cls in (
        APIWorker,
        AuthWorker,
        ServiceWorker,
        UIWorker,
        ComponentWorker,
        SchemaWorker,
        MigrationWorker,
        ModelWorker,
        EvaluationWorker,
    )
}
