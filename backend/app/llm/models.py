"""Shared data types for the LLM Gateway (Phase 4).

`LLMSettings` is defined in `app.llm.config` (it needs `pydantic_settings`
env-loading machinery) and re-exported here so callers/tests can import all
gateway-related types from a single module: `app.llm.models`.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from app.llm.config import LLMSettings, get_settings

__all__ = [
    "LLMSettings",
    "LLMTelemetryRecord",
    "TaskType",
    "get_settings",
]


class TaskType(str, Enum):
    """Every kind of work the orchestration layer can hand to an LLM.

    Routing to a concrete model is deterministic and lives in
    `app.llm.gateway.LLMGateway.route_model` -- this enum only names the
    task, it never carries a model name.
    """

    # -- Reasoning model (Qwen3) ------------------------------------
    REQUIREMENTS = "REQUIREMENTS"
    ARCHITECTURE = "ARCHITECTURE"
    PLANNING = "PLANNING"
    DECOMPOSITION = "DECOMPOSITION"
    MANAGEMENT = "MANAGEMENT"
    REASONING = "REASONING"
    ERROR_ANALYSIS = "ERROR_ANALYSIS"
    INTEGRATION_REASONING = "INTEGRATION_REASONING"
    ROUTING = "ROUTING"

    # -- Coding model (Qwen2.5-Coder) ---------------------------------
    CODING = "CODING"
    DEBUGGING = "DEBUGGING"
    CODE_REVIEW = "CODE_REVIEW"
    TEST_GENERATION = "TEST_GENERATION"
    REFACTORING = "REFACTORING"
    DOCUMENTATION = "DOCUMENTATION"
    CONFIGURATION = "CONFIGURATION"


class LLMTelemetryRecord(BaseModel):
    """Observability record captured for every LLM Gateway call.

    Intentionally contains no prompt/response text -- only metadata -- so
    that logging this record can never leak secrets embedded in a prompt.
    """

    request_id: str
    project_id: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    task_type: TaskType
    selected_model: str
    start_time: float
    end_time: float
    duration: float
    success: bool
    error_message: str | None = None
