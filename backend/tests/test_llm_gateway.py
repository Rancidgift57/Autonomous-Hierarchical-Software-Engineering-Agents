import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from app.llm.exceptions import (
    InvalidJSONError,
    InvalidTaskTypeError,
    LLMTimeoutError,
    ModelNotFoundError,
    OllamaUnavailableError,
)
from app.llm.gateway import LLMGateway
from app.llm.models import LLMSettings, TaskType


class SampleSchema(BaseModel):
    status: str
    code: int

@pytest.fixture
def mock_provider():
    provider = AsyncMock()
    provider.generate = AsyncMock(return_value='{"status": "ok", "code": 200}')
    provider.health_check = AsyncMock(return_value=True)
    return provider

@pytest.fixture
def gateway(mock_provider):
    settings = LLMSettings(
        ollama_reasoning_model="qwen3:8b",
        ollama_coding_model="qwen2.5-coder:7b",
        max_llm_concurrency=1,
        model_keep_alive=0
    )
    return LLMGateway(provider=mock_provider, settings=settings)

@pytest.mark.asyncio
async def test_qwen3_routing(gateway):
    model = gateway.route_model(TaskType.ARCHITECTURE)
    assert model == "qwen3:8b"

    resp = await gateway.generate(task_type=TaskType.ARCHITECTURE, prompt="Design a system.")
    assert resp == '{"status": "ok", "code": 200}'
    gateway.provider.generate.assert_awaited_once()

@pytest.mark.asyncio
async def test_coder_routing(gateway):
    model = gateway.route_model(TaskType.CODING)
    assert model == "qwen2.5-coder:7b"

    resp = await gateway.generate(task_type=TaskType.CODING, prompt="Write a function.")
    assert resp == '{"status": "ok", "code": 200}'

@pytest.mark.asyncio
async def test_invalid_task_type(gateway):
    with pytest.raises(InvalidTaskTypeError):
        gateway.route_model("INVALID_TASK") # type: ignore

@pytest.mark.asyncio
async def test_timeout_handling(gateway, mock_provider):
    mock_provider.generate.side_effect = LLMTimeoutError("Timed out")
    with pytest.raises(LLMTimeoutError):
        await gateway.generate(TaskType.REASONING, "Think about this.")

@pytest.mark.asyncio
async def test_ollama_unavailable(gateway, mock_provider):
    mock_provider.generate.side_effect = OllamaUnavailableError("Service down")
    with pytest.raises(OllamaUnavailableError):
        await gateway.generate(TaskType.CODING, "Code this.")

@pytest.mark.asyncio
async def test_malformed_json_and_retry(gateway, mock_provider):
    # First two calls return bad JSON, third call returns valid JSON
    mock_provider.generate.side_effect = [
        "Not a JSON response",
        '{"status": "almost"}',
        '{"status": "ok", "code": 200}'
    ]
    result = await gateway.generate_json(
        task_type=TaskType.CODING,
        prompt="Generate JSON",
        response_model=SampleSchema,
        max_retries=2
    )
    assert result.status == "ok"
    assert result.code == 200
    assert mock_provider.generate.call_count == 3

@pytest.mark.asyncio
async def test_json_retry_exhaustion(gateway, mock_provider):
    mock_provider.generate.side_effect = [
        "Bad JSON 1",
        "Bad JSON 2",
        "Bad JSON 3"
    ]
    with pytest.raises(InvalidJSONError):
        await gateway.generate_json(
            task_type=TaskType.CODING,
            prompt="Generate JSON",
            response_model=SampleSchema,
            max_retries=2
        )

@pytest.mark.asyncio
async def test_model_not_found(gateway, mock_provider):
    mock_provider.generate.side_effect = ModelNotFoundError("Model not pulled")
    with pytest.raises(ModelNotFoundError):
        await gateway.generate(TaskType.CODING, "Code this.")

@pytest.mark.asyncio
async def test_concurrency_limiting(gateway, mock_provider):
    # Test that concurrency semaphore works (MAX_LLM_CONCURRENCY = 1)
    assert gateway.semaphore._value == 1

@pytest.mark.asyncio
async def test_concurrency_serializes_calls(mock_provider):
    """With MAX_LLM_CONCURRENCY=1, two concurrent generate() calls must not
    run inference at the same time -- the second only starts once the first
    has released the semaphore."""

    settings = LLMSettings(max_llm_concurrency=1)
    gateway = LLMGateway(provider=mock_provider, settings=settings)

    active = 0
    max_active = 0

    async def slow_generate(**kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        return '{"status": "ok", "code": 200}'

    mock_provider.generate = AsyncMock(side_effect=slow_generate)

    await asyncio.gather(
        gateway.generate(TaskType.CODING, "task one"),
        gateway.generate(TaskType.REASONING, "task two"),
    )

    assert max_active == 1

@pytest.mark.asyncio
async def test_health_check(gateway, mock_provider):
    healthy = await gateway.health_check()
    assert healthy is True

@pytest.mark.asyncio
async def test_health_check_failure(gateway, mock_provider):
    mock_provider.health_check = AsyncMock(return_value=False)
    healthy = await gateway.health_check()
    assert healthy is False