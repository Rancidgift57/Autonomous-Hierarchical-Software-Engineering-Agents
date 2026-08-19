"""Dual local LLM Gateway (Phase 4).

Exposes the public surface agents/orchestration code should depend on:
`LLMGateway`, `TaskType`, the provider abstraction, settings, and the
gateway's exception hierarchy. No code outside this package should talk to
`OllamaProvider` or Ollama's HTTP API directly -- always go through
`LLMGateway`.
"""

from app.llm.config import LLMSettings, get_settings
from app.llm.exceptions import (
    ConnectionError,
    InferenceFailureError,
    InvalidJSONError,
    InvalidTaskTypeError,
    LLMError,
    LLMTimeoutError,
    MalformedResponseError,
    ModelNotFoundError,
    OllamaUnavailableError,
)
from app.llm.gateway import LLMGateway
from app.llm.models import LLMTelemetryRecord, TaskType
from app.llm.provider import LLMProvider, OllamaProvider

__all__ = [
    "ConnectionError",
    "InferenceFailureError",
    "InvalidJSONError",
    "InvalidTaskTypeError",
    "LLMError",
    "LLMGateway",
    "LLMProvider",
    "LLMSettings",
    "LLMTelemetryRecord",
    "LLMTimeoutError",
    "MalformedResponseError",
    "ModelNotFoundError",
    "OllamaProvider",
    "OllamaUnavailableError",
    "TaskType",
    "get_settings",
]
