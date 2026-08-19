"""Environment-driven configuration for the LLM Gateway (Phase 4).

Every knob here is read from the environment (or a local `.env` file) --
nothing about model names, timeouts, or concurrency is hard-coded into the
gateway or provider logic. The defaults below match the values specified
for local development and only apply when the corresponding environment
variable is unset.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseSettings):
    """Runtime configuration for `OllamaProvider` / `LLMGateway`."""

    ollama_base_url: str = Field(
        default="http://localhost:11434", validation_alias="OLLAMA_BASE_URL"
    )
    ollama_reasoning_model: str = Field(
        default="qwen3:4b", validation_alias="OLLAMA_REASONING_MODEL"
    )
    ollama_coding_model: str = Field(
        default="qwen2.5-coder:7b-instruct-q4_K_M", validation_alias="OLLAMA_CODING_MODEL"
    )
    llm_request_timeout: int = Field(default=300, validation_alias="LLM_REQUEST_TIMEOUT")
    max_llm_concurrency: int = Field(default=1, validation_alias="MAX_LLM_CONCURRENCY")
    model_keep_alive: int = Field(default=0, validation_alias="MODEL_KEEP_ALIVE")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> LLMSettings:
    """Return process-wide cached settings, read once from the environment.

    Cached with `lru_cache` so repeated calls (e.g. from multiple agents)
    don't re-parse the environment/`.env` file on every request. Tests that
    need different settings should construct `LLMSettings(...)` directly
    instead of relying on this cache.
    """

    return LLMSettings()
