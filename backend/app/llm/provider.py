"""LLM provider abstraction (Phase 4).

`LLMProvider` is the abstract interface every backend must implement.
`OllamaProvider` is the concrete implementation that talks to a local
Ollama server over HTTP. Agents/gateway callers never talk to
`OllamaProvider` (or Ollama's HTTP API) directly -- only `LLMGateway` does.
"""

from __future__ import annotations

import abc
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, TypeVar

import httpx

from app.llm.exceptions import (
    ConnectionError,
    InferenceFailureError,
    LLMTimeoutError,
    MalformedResponseError,
    ModelNotFoundError,
    OllamaUnavailableError,
)

T = TypeVar("T")


class LLMProvider(abc.ABC):
    """Abstract interface for a local/remote LLM inference backend."""

    @abc.abstractmethod
    async def generate(
        self,
        model: str,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        keep_alive: int = 0,
        timeout: int = 300,
    ) -> str:
        raise NotImplementedError

    @abc.abstractmethod
    def stream(
        self,
        model: str,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        keep_alive: int = 0,
        timeout: int = 300,
    ) -> AsyncGenerator[str, None]:
        raise NotImplementedError

    @abc.abstractmethod
    async def health_check(self) -> bool:
        raise NotImplementedError


class OllamaProvider(LLMProvider):
    """`LLMProvider` implementation backed by a local Ollama server."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Error translation
    # ------------------------------------------------------------------

    async def _guarded(self, call: Callable[[], Awaitable[T]]) -> T:
        """Run an httpx call, translating transport errors into LLM* exceptions."""

        try:
            return await call()
        except httpx.TimeoutException as e:
            raise LLMTimeoutError(f"Ollama request timed out: {e}") from e
        except httpx.ConnectError as e:
            raise OllamaUnavailableError(
                f"Could not connect to Ollama at {self.base_url}: {e}"
            ) from e
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ModelNotFoundError(f"Model not found: {e.response.text}") from e
            raise InferenceFailureError(
                f"Ollama inference failed with status {e.response.status_code}: "
                f"{e.response.text}"
            ) from e
        except httpx.RequestError as e:
            raise ConnectionError(f"Network error communicating with Ollama: {e}") from e

    @staticmethod
    def _build_payload(
        model: str,
        prompt: str,
        *,
        stream: bool,
        system_prompt: str | None,
        temperature: float | None,
        max_tokens: int | None,
        keep_alive: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": stream,
            "keep_alive": keep_alive,
        }
        if system_prompt:
            payload["system"] = system_prompt

        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        if options:
            payload["options"] = options
        return payload

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate(
        self,
        model: str,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        keep_alive: int = 0,
        timeout: int = 300,
    ) -> str:
        url = f"{self.base_url}/api/generate"
        payload = self._build_payload(
            model,
            prompt,
            stream=False,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            keep_alive=keep_alive,
        )

        async def _call() -> httpx.Response:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                return response

        response = await self._guarded(_call)
        try:
            data = response.json()
        except json.JSONDecodeError as e:
            raise MalformedResponseError(
                f"Ollama returned non-JSON response body: {e}"
            ) from e

        response_text = str(data.get("response", "")).strip()
        if not response_text:
            raise MalformedResponseError("Received empty response from Ollama model.")
        return response_text

    async def stream(
        self,
        model: str,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        keep_alive: int = 0,
        timeout: int = 300,
    ) -> AsyncGenerator[str, None]:
        url = f"{self.base_url}/api/generate"
        payload = self._build_payload(
            model,
            prompt,
            stream=True,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            keep_alive=keep_alive,
        )

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        chunk = json.loads(line)
                        token = chunk.get("response", "")
                        if token:
                            yield token
        except httpx.TimeoutException as e:
            raise LLMTimeoutError(f"Ollama stream timed out: {e}") from e
        except httpx.ConnectError as e:
            raise OllamaUnavailableError(
                f"Could not connect to Ollama stream at {self.base_url}: {e}"
            ) from e
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ModelNotFoundError(f"Model not found: {e.response.text}") from e
            raise InferenceFailureError(
                f"Ollama inference failed with status {e.response.status_code}: "
                f"{e.response.text}"
            ) from e
        except httpx.RequestError as e:
            raise ConnectionError(f"Network error communicating with Ollama stream: {e}") from e

    async def health_check(self) -> bool:
        url = f"{self.base_url}/api/tags"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url)
                return response.status_code == 200
        except httpx.HTTPError:
            return False
