"""LLM Gateway (Phase 4).

`LLMGateway` is the single, mandatory entry point for every LLM call made
anywhere in AHSEA. Agents (and everything else) must depend on this class
-- and the `TaskType` enum -- never on `OllamaProvider` or Ollama's HTTP
API directly.

Responsibilities:
    * Deterministic task_type -> model routing (`route_model`).
    * Enforcing a single global concurrency limit across both models, since
      the dev machine has 6 GB VRAM and can't reliably hold two 7-8B models
      resident at once (`MAX_LLM_CONCURRENCY`, default 1).
    * Reliable structured (JSON) output: request -> parse -> validate ->
      retry with a correction prompt -> raise after retries are exhausted.
    * Recording an `LLMTelemetryRecord` for every call, without ever
      logging prompt/response text (so secrets embedded in prompts can't
      leak into logs).

Model lifecycle: with `MODEL_KEEP_ALIVE=0` (the default), Ollama unloads a
model from VRAM immediately after each request completes, so the gateway
does not need to explicitly juggle which model is resident -- it just asks
Ollama for whichever model the task requires, and the low keep-alive
setting keeps the two models from fighting for the same 6 GB of VRAM.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.llm.exceptions import InvalidJSONError, InvalidTaskTypeError, LLMError
from app.llm.models import LLMSettings, LLMTelemetryRecord, TaskType
from app.llm.provider import LLMProvider

logger = logging.getLogger("ahsea.llm.gateway")

ModelT = TypeVar("ModelT", bound=BaseModel)

# ---------------------------------------------------------------------------
# Deterministic routing table
# ---------------------------------------------------------------------------

#: Task types handled by the reasoning model (Qwen3): requirements analysis,
#: project understanding, architecture, planning, task decomposition,
#: management, delegation/decision reasoning, error analysis, integration
#: reasoning, and self-healing diagnosis. ROUTING (deciding how to delegate
#: work) is decision-making, so it belongs here too.
REASONING_TASK_TYPES: frozenset[TaskType] = frozenset(
    {
        TaskType.REQUIREMENTS,
        TaskType.ARCHITECTURE,
        TaskType.PLANNING,
        TaskType.DECOMPOSITION,
        TaskType.MANAGEMENT,
        TaskType.REASONING,
        TaskType.ERROR_ANALYSIS,
        TaskType.INTEGRATION_REASONING,
        TaskType.ROUTING,
    }
)

#: Task types handled by the coding model (Qwen2.5-Coder): code generation,
#: implementation, debugging, code review, test generation, refactoring,
#: documentation of generated code, and configuration file work.
CODING_TASK_TYPES: frozenset[TaskType] = frozenset(
    {
        TaskType.CODING,
        TaskType.DEBUGGING,
        TaskType.CODE_REVIEW,
        TaskType.TEST_GENERATION,
        TaskType.REFACTORING,
        TaskType.DOCUMENTATION,
        TaskType.CONFIGURATION,
    }
)


class LLMGateway:
    """The only supported way to call an LLM anywhere in AHSEA."""

    def __init__(
        self,
        provider: LLMProvider,
        settings: LLMSettings | None = None,
        telemetry_sink: Callable[[LLMTelemetryRecord], Awaitable[None]] | None = None,
        observability_sink: Callable[[LLMTelemetryRecord], Awaitable[None]] | None = None,
    ):
        self.provider = provider
        self.settings = settings or LLMSettings()
        self.semaphore = asyncio.Semaphore(self.settings.max_llm_concurrency)
        # Optional Phase 17 hook: if set, every `LLMTelemetryRecord` this
        # gateway produces is also handed to `telemetry_sink` (e.g.
        # `PersistenceService.record_llm_request`) for durable storage.
        # Fire-and-forget by design -- persistence should never add latency
        # or a failure mode to an LLM call.
        self.telemetry_sink = telemetry_sink
        self.observability_sink = observability_sink

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def route_model(self, task_type: TaskType) -> str:
        """Deterministically map a `TaskType` to a concrete Ollama model tag.

        Raises:
            InvalidTaskTypeError: if `task_type` is not a `TaskType` member.
        """

        if not isinstance(task_type, TaskType):
            raise InvalidTaskTypeError(f"Unrecognized task type: {task_type!r}")

        if task_type in REASONING_TASK_TYPES:
            return self.settings.ollama_reasoning_model
        if task_type in CODING_TASK_TYPES:
            return self.settings.ollama_coding_model

        # Defensive: every TaskType member must be routed. If a new member
        # is added to the enum without updating the routing tables above,
        # fail loudly rather than silently guessing a model.
        raise InvalidTaskTypeError(
            f"Task type {task_type!r} has no routing rule configured."
        )

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    def _record_telemetry(
        self,
        *,
        request_id: str,
        task_type: TaskType,
        model: str,
        start_time: float,
        end_time: float,
        success: bool,
        metadata: dict[str, Any] | None,
        error_message: str | None = None,
    ) -> None:
        metadata = metadata or {}
        record = LLMTelemetryRecord(
            request_id=request_id,
            project_id=metadata.get("project_id"),
            agent_id=metadata.get("agent_id"),
            task_id=metadata.get("task_id"),
            task_type=task_type,
            selected_model=model,
            start_time=start_time,
            end_time=end_time,
            duration=end_time - start_time,
            success=success,
            error_message=error_message,
        )
        # Only ever log the structured metadata record above -- never the
        # prompt or response text, which may contain secrets.
        log_fn = logger.info if success else logger.warning
        log_fn("llm_call", extra={"llm_telemetry": record.model_dump(mode="json")})

        if self.telemetry_sink is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                task = loop.create_task(self.telemetry_sink(record))
                task.add_done_callback(self._log_telemetry_sink_failure)
        if self.observability_sink is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                task = loop.create_task(self.observability_sink(record))
                task.add_done_callback(self._log_telemetry_sink_failure)

    @staticmethod
    def _log_telemetry_sink_failure(task: asyncio.Task) -> None:
        exc = task.exception() if not task.cancelled() else None
        if exc is not None:
            logger.warning("llm_telemetry_sink_failed", exc_info=exc)

    # ------------------------------------------------------------------
    # Core generation
    # ------------------------------------------------------------------

    async def generate(
        self,
        task_type: TaskType,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Generate a plain-text completion for `task_type`, routed automatically.

        The caller specifies *what kind of task* it needs done, not which
        model to use -- e.g. `TaskType.ARCHITECTURE` always uses Qwen3,
        `TaskType.CODING` always uses Qwen2.5-Coder.
        """

        model = self.route_model(task_type)
        request_id = str(uuid.uuid4())
        start_time = time.monotonic()

        async with self.semaphore:
            try:
                result = await self.provider.generate(
                    model=model,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    keep_alive=self.settings.model_keep_alive,
                    timeout=self.settings.llm_request_timeout,
                )
            except LLMError as exc:
                self._record_telemetry(
                    request_id=request_id,
                    task_type=task_type,
                    model=model,
                    start_time=start_time,
                    end_time=time.monotonic(),
                    success=False,
                    metadata=metadata,
                    error_message=str(exc),
                )
                raise

        self._record_telemetry(
            request_id=request_id,
            task_type=task_type,
            model=model,
            start_time=start_time,
            end_time=time.monotonic(),
            success=True,
            metadata=metadata,
        )
        return result

    async def stream(
        self,
        task_type: TaskType,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncGenerator[str, None]:
        """Stream a completion for `task_type`, routed automatically."""

        model = self.route_model(task_type)
        request_id = str(uuid.uuid4())
        start_time = time.monotonic()

        async with self.semaphore:
            try:
                async for token in self.provider.stream(
                    model=model,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    keep_alive=self.settings.model_keep_alive,
                    timeout=self.settings.llm_request_timeout,
                ):
                    yield token
            except LLMError as exc:
                self._record_telemetry(
                    request_id=request_id,
                    task_type=task_type,
                    model=model,
                    start_time=start_time,
                    end_time=time.monotonic(),
                    success=False,
                    metadata=metadata,
                    error_message=str(exc),
                )
                raise
            else:
                self._record_telemetry(
                    request_id=request_id,
                    task_type=task_type,
                    model=model,
                    start_time=start_time,
                    end_time=time.monotonic(),
                    success=True,
                    metadata=metadata,
                )

    # ------------------------------------------------------------------
    # Structured (JSON) generation
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_json(raw: str) -> Any:
        """Best-effort extraction of a JSON value from raw model output.

        Models often wrap JSON in ```json fences or add stray prose. We try
        a strict parse first, then fall back to slicing out the first
        `{...}` or `[...]` block before giving up.
        """

        text = raw.strip()
        # Strip ```json ... ``` or ``` ... ``` fences if present.
        fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
        if fence_match:
            text = fence_match.group(1).strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        for open_ch, close_ch in (("{", "}"), ("[", "]")):
            start = text.find(open_ch)
            end = text.rfind(close_ch)
            if start != -1 and end != -1 and end > start:
                candidate = text[start : end + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    continue

        # Let the caller's except clause handle this uniformly.
        raise json.JSONDecodeError("No valid JSON object/array found", text, 0)

    @staticmethod
    def _build_json_prompt(prompt: str, response_model: type[BaseModel]) -> str:
        schema = json.dumps(response_model.model_json_schema())
        return (
            f"{prompt}\n\n"
            "Respond with ONLY a single valid JSON object matching this JSON "
            f"Schema, and nothing else (no markdown fences, no commentary):\n{schema}"
        )

    @staticmethod
    def _build_correction_prompt(
        original_prompt: str,
        response_model: type[BaseModel],
        bad_output: str,
        error: Exception,
    ) -> str:
        schema = json.dumps(response_model.model_json_schema())
        return (
            f"{original_prompt}\n\n"
            "Your previous response was not valid JSON matching the required "
            f"schema.\nPrevious response:\n{bad_output}\n\n"
            f"Validation error:\n{error}\n\n"
            "Respond again with ONLY a single valid JSON object matching this "
            f"JSON Schema, and nothing else (no markdown fences, no commentary):\n{schema}"
        )

    async def generate_json(
        self,
        task_type: TaskType,
        prompt: str,
        response_model: type[ModelT],
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_retries: int = 3,
        metadata: dict[str, Any] | None = None,
    ) -> ModelT:
        """Generate JSON for `task_type` and validate it against `response_model`.

        On malformed output (invalid JSON, or JSON that fails Pydantic
        validation) the gateway retries up to `max_retries` additional
        times, each time sending the model its previous bad output plus the
        validation error as a correction prompt. After retries are
        exhausted, raises `InvalidJSONError`.
        """

        current_prompt = self._build_json_prompt(prompt, response_model)
        last_error: Exception | None = None
        last_raw: str | None = None

        for attempt in range(max_retries + 1):
            raw = await self.generate(
                task_type=task_type,
                prompt=current_prompt,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                metadata=metadata,
            )
            last_raw = raw

            try:
                parsed = self._extract_json(raw)
                return response_model.model_validate(parsed)
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = exc
                if attempt < max_retries:
                    current_prompt = self._build_correction_prompt(
                        prompt, response_model, raw, exc
                    )
                continue

        raise InvalidJSONError(
            f"Failed to obtain JSON valid for schema '{response_model.__name__}' "
            f"after {max_retries + 1} attempt(s). Last error: {last_error}. "
            f"Last raw output: {last_raw!r}"
        )

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Check whether the underlying Ollama server is reachable."""

        return await self.provider.health_check()
