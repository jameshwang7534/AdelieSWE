"""Async generation tests use mocked HTTP and deterministic JSON fixtures only."""

import asyncio
import json
import logging
from functools import partial
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from app.core.config import Settings
from app.integrations.llm.fake_provider import FakeLLMProvider
from app.integrations.llm.openai_provider import OpenAICompatibleLLMProvider
from app.integrations.llm.provider import LLMError, TokenUsage
from app.integrations.llm.structured_schema import structured_schema
from app.services.llm_diagnostic import DiagnosticResponse, run_diagnostic
from app.services.llm_resources import llm_resources


@pytest.fixture(autouse=True)
def block_real_http_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx.AsyncHTTPTransport,
        "handle_async_request",
        AsyncMock(side_effect=AssertionError("Real HTTP is forbidden in unit tests")),
    )
    monkeypatch.setattr(
        httpx.HTTPTransport,
        "handle_request",
        lambda *args, **kwargs: pytest.fail("Real HTTP is forbidden in unit tests"),
    )


@pytest.mark.asyncio
async def test_environment_configuration_reaches_production_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "LLM_API_KEY": "synthetic-environment-fixture",
        "LLM_MODEL": "configured-fixture",
        "LLM_BASE_URL": "https://example.invalid/custom/v1",
        "LLM_TIMEOUT_SECONDS": "7",
        "LLM_MAX_RETRIES": "1",
        "LLM_MAX_OUTPUT_TOKENS": "123",
    }.items():
        monkeypatch.setenv(name, value)

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.invalid/custom/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer synthetic-environment-fixture"
        payload = json.loads(request.content)
        assert payload["model"] == "configured-fixture"
        assert payload["max_completion_tokens"] == 123
        assert request.extensions["timeout"]["read"] == 7
        return httpx.Response(200, json=completion())

    factory = partial(httpx.AsyncClient, transport=httpx.MockTransport(respond))
    with patch("app.services.llm_resources.httpx.AsyncClient", side_effect=factory):
        async with llm_resources(Settings()) as provider:
            assert (await run_diagnostic(provider)).output.status == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["timeout", "network"])
async def test_exhausted_transport_errors_are_sanitized(
    kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    sentinel = "synthetic-transport-secret"

    def respond(request: httpx.Request) -> httpx.Response:
        error = httpx.ReadTimeout if kind == "timeout" else httpx.ConnectError
        raise error(sentinel, request=request)

    async with httpx.AsyncClient(
        base_url="https://example.invalid/", transport=httpx.MockTransport(respond)
    ) as client:
        with pytest.raises(LLMError) as error:
            await run_diagnostic(OpenAICompatibleLLMProvider(client, "fixture", max_retries=0))
    assert error.value.code == ("llm_timeout" if kind == "timeout" else "llm_unavailable")
    assert sentinel not in str(error.value) and sentinel not in caplog.text


def completion(content: str = '{"status":"ok"}') -> dict[str, object]:
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": content}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base",
    [
        "http://example.invalid",
        "https://example.invalid:bad",
        "https://example.invalid:99999",
        "https://[invalid",
        "https://user:synthetic@example.invalid",
        "https://example.invalid?key=synthetic",
    ],
)
async def test_invalid_configuration_is_sanitized(base: str) -> None:
    with pytest.raises(LLMError, match="^llm_invalid_configuration$"):
        async with llm_resources(
            Settings(llm_api_key=SecretStr("synthetic"), llm_model="fixture", llm_base_url=base)
        ):
            pytest.fail("Invalid endpoint must not create a provider")


@pytest.mark.asyncio
async def test_fake_diagnostic() -> None:
    result = await run_diagnostic(FakeLLMProvider('{"status":"ok"}', TokenUsage(input_tokens=3)))
    assert isinstance(result.output, DiagnosticResponse) and result.output.status == "ok"
    assert result.usage.input_tokens == 3 and result.usage.output_tokens is None


@pytest.mark.asyncio
async def test_http_request_and_usage(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    sentinel = "synthetic-secret-not-a-real-key"

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {sentinel}"
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "fixture-model" and body["max_completion_tokens"] == 256
        assert body["messages"] == [
            {"role": "user", "content": 'Return JSON with status set to "ok".'}
        ]
        schema = body["response_format"]["json_schema"]
        assert schema["strict"] and schema["schema"]["additionalProperties"] is False
        assert schema["schema"]["required"] == ["status"]
        assert request.extensions["timeout"]["read"] == 2
        return httpx.Response(200, json=completion())

    async with httpx.AsyncClient(
        base_url="https://example.invalid/v1/",
        headers={"Authorization": f"Bearer {sentinel}"},
        transport=httpx.MockTransport(respond),
    ) as client:
        result = await run_diagnostic(
            OpenAICompatibleLLMProvider(client, "fixture-model", timeout=2, max_output_tokens=256)
        )
    assert result.output.status == "ok"
    assert result.usage == TokenUsage(input_tokens=12, output_tokens=4, total_tokens=16)
    assert sentinel not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [429, 500, 408, "network", "timeout"])
async def test_retry_recovery(failure: int | str) -> None:
    calls = 0
    sleep = AsyncMock()

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "network":
                raise httpx.ConnectError("secret", request=request)
            if failure == "timeout":
                raise httpx.ReadTimeout("secret", request=request)
            return httpx.Response(int(failure), headers={"Retry-After": "2"})
        return httpx.Response(200, json=completion())

    async with httpx.AsyncClient(
        base_url="https://example.invalid/", transport=httpx.MockTransport(respond)
    ) as client:
        result = await run_diagnostic(OpenAICompatibleLLMProvider(client, "test", sleep=sleep))
    assert result.output.status == "ok" and calls == 2
    sleep.assert_awaited_once_with(1.0 if isinstance(failure, str) else 2.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code,calls",
    [
        (401, "llm_authentication_failed", 1),
        (403, "llm_authentication_failed", 1),
        (400, "llm_request_rejected", 1),
        (302, "llm_request_rejected", 1),
        (429, "llm_rate_limited", 3),
        (503, "llm_unavailable", 3),
    ],
)
async def test_sanitized_failures(
    status: int, code: str, calls: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    seen = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        return httpx.Response(status, text="synthetic-secret")

    async with httpx.AsyncClient(
        base_url="https://example.invalid/", transport=httpx.MockTransport(respond)
    ) as client:
        with pytest.raises(LLMError, match=f"^{code}$"):
            await run_diagnostic(
                OpenAICompatibleLLMProvider(client, "test", max_retries=2, sleep=AsyncMock())
            )
    assert seen == calls and "synthetic-secret" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,code",
    [
        ({}, "llm_invalid_response"),
        (completion("not json"), "llm_invalid_response"),
        (completion('{"status":"bad"}'), "llm_invalid_response"),
        (
            {"choices": [{"finish_reason": "stop", "message": {"refusal": "private text"}}]},
            "llm_refused",
        ),
        (
            {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
            "llm_incomplete",
        ),
    ],
)
async def test_invalid_output_not_retried(payload: object, code: str) -> None:
    sleep = AsyncMock()
    async with httpx.AsyncClient(
        base_url="https://example.invalid/",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    ) as client:
        with pytest.raises(LLMError, match=f"^{code}$"):
            await run_diagnostic(OpenAICompatibleLLMProvider(client, "test", sleep=sleep))
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_timeout_and_cancellation() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(200, json=completion())

    async with httpx.AsyncClient(
        base_url="https://example.invalid/", transport=httpx.MockTransport(slow)
    ) as client:
        with pytest.raises(LLMError, match="llm_timeout"):
            await run_diagnostic(
                OpenAICompatibleLLMProvider(client, "test", timeout=0.01, max_retries=0)
            )
        task = asyncio.create_task(run_diagnostic(OpenAICompatibleLLMProvider(client, "test")))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_missing_credentials_and_resource_lifecycle() -> None:
    with pytest.raises(LLMError, match="llm_unconfigured"):
        async with llm_resources(Settings(llm_api_key=None, llm_model=None)):
            pytest.fail("Must not yield production provider")
    client = httpx.AsyncClient(
        base_url="https://example.invalid/",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": '{"status":"ok"}'}}
                    ]
                },
            )
        ),
    )
    with patch("app.services.llm_resources.httpx.AsyncClient", return_value=client):
        async with llm_resources(
            Settings(llm_api_key=SecretStr("synthetic"), llm_model="fixture")
        ) as provider:
            assert (await run_diagnostic(provider)).usage == TokenUsage()
    assert client.is_closed


def test_nested_schema_and_unconstrained_map_rejection() -> None:
    class Nested(BaseModel):
        child: DiagnosticResponse
        optional: str | None = None

    schema = structured_schema(Nested)
    assert schema["required"] == ["child", "optional"]
    assert "default" not in schema["properties"]["optional"]
    assert schema["$defs"]["DiagnosticResponse"]["additionalProperties"] is False

    class Unconstrained(BaseModel):
        data: dict[str, str]

    with pytest.raises(LLMError, match="llm_invalid_configuration"):
        structured_schema(Unconstrained)


@pytest.mark.asyncio
async def test_fake_validates_schema_and_empty_messages() -> None:
    with pytest.raises(LLMError, match="llm_invalid_response"):
        await run_diagnostic(FakeLLMProvider('{"status":42}'))
    with pytest.raises(LLMError, match="llm_invalid_configuration"):
        await FakeLLMProvider("{}").generate([], DiagnosticResponse)
