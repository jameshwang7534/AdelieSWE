"""Provider contract tests without network access or real credentials."""

import json
import logging
from unittest.mock import Mock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.integrations.llm.embeddings import EmbeddingError, validate_vectors
from app.integrations.llm.fake_embeddings import FakeEmbeddingProvider
from app.integrations.llm.openai_embeddings import OpenAIEmbeddingProvider
from app.main import create_app
from app.services.embedding_resources import embedding_resources
from app.workers.embeddings import embed_code


def test_batching_and_response_order() -> None:
    sizes: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/v1/embeddings"
        assert body["model"] == "fixture" and body["encoding_format"] == "float"
        assert body["dimensions"] == 2
        sizes.append(len(body["input"]))
        return httpx.Response(
            200,
            json={"data": [{"index": i, "embedding": [1, i]} for i in reversed(range(sizes[-1]))]},
        )

    with httpx.Client(
        base_url="https://example.invalid/v1/", transport=httpx.MockTransport(respond)
    ) as client:
        provider = OpenAIEmbeddingProvider(client, "fixture", 2, batch_size=2)
        result = provider.embed(["a", "b", "c"])
        assert sizes == [2, 1]
        assert result[0] == result[2] == [1, 0]
        assert result[1] == pytest.approx([2**-0.5, 2**-0.5])
        assert provider.embed([]) == []


@pytest.mark.parametrize("failure", [429, 503, "network"])
def test_transient_retry_and_exhaustion(failure: int | str) -> None:
    calls = 0
    sleep = Mock()

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if failure == "network":
            raise httpx.ConnectError("synthetic-sensitive-detail", request=request)
        return httpx.Response(int(failure), headers={"Retry-After": "2"})

    with httpx.Client(
        base_url="https://example.invalid/", transport=httpx.MockTransport(respond)
    ) as client:
        provider = OpenAIEmbeddingProvider(client, "fixture", 2, max_retries=2, sleep=sleep)
        with pytest.raises(EmbeddingError, match="^embedding_unavailable$"):
            provider.embed(["hello"])
    assert calls == 3 and sleep.call_count == 2
    assert sleep.call_args_list[-1].args == (2.0,)


def test_retry_recovers_and_optional_dimensions() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert "dimensions" not in json.loads(request.content)
        return (
            httpx.Response(500)
            if calls == 1
            else httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]})
        )

    with httpx.Client(
        base_url="https://example.invalid/", transport=httpx.MockTransport(respond)
    ) as client:
        provider = OpenAIEmbeddingProvider(
            client, "fixture", 2, sleep=Mock(), send_dimensions=False
        )
        assert provider.embed(["hello"]) == [[1, 0]]
    assert calls == 2


@pytest.mark.parametrize("status", [401, 403, 400, 302])
def test_rejection_is_sanitized(status: int, caplog: pytest.LogCaptureFixture) -> None:
    handler = Mock(return_value=httpx.Response(status, text="synthetic-secret-token"))
    with httpx.Client(
        base_url="https://example.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(EmbeddingError, match="^embedding_request_rejected$"):
            OpenAIEmbeddingProvider(client, "fixture", 2).embed(["hello"])
    assert handler.call_count == 1
    assert "synthetic-secret-token" not in caplog.text


@pytest.mark.parametrize(
    "vectors", [[], [[1]], [[0, 0]], [[float("nan"), 1]], [[float("inf"), 1]], [[True, 1]]]
)
def test_invalid_vectors(vectors: object) -> None:
    with pytest.raises(EmbeddingError):
        validate_vectors(vectors, 1, 2)


@pytest.mark.parametrize(
    "payload", [{}, {"data": []}, {"data": [{"index": 1, "embedding": [1, 0]}]}]
)
def test_invalid_provider_payload(payload: object) -> None:
    with httpx.Client(
        base_url="https://example.invalid/",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    ) as client:
        with pytest.raises(EmbeddingError):
            OpenAIEmbeddingProvider(client, "fixture", 2).embed(["hello"])


def test_fake_is_deterministic_and_configuration_optional() -> None:
    assert FakeEmbeddingProvider().embed(["getUser"]) == FakeEmbeddingProvider().embed(["getUser"])
    with embedding_resources(Settings(llm_api_key=None, embedding_model=None)) as provider:
        assert provider is None
    with pytest.raises(EmbeddingError):
        with embedding_resources(
            Settings(llm_api_key=SecretStr("synthetic"), embedding_model="test", embedding_dim=2)
        ):
            pytest.fail("Invalid dimensions must fail before a request")


def test_vector_endpoint_without_credentials() -> None:
    settings = Settings(database_url=None, redis_url=None, celery_broker_url=None, llm_api_key=None)
    with TestClient(create_app(settings)) as api:
        response = api.post(
            "/repositories/00000000-0000-0000-0000-000000000001/search/vector",
            json={"query": "user", "top_k": 1},
        )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "vector_search_unconfigured"
        assert api.get("/health").status_code == 200


def test_worker_missing_configuration_is_actionable() -> None:
    settings = Settings(llm_api_key=None, embedding_model="fixture")
    with (
        patch("app.workers.embeddings.Settings", return_value=settings),
        patch("app.db.session.create_database_engine") as engine,
        pytest.raises(EmbeddingError, match="embedding_unconfigured: set LLM_API_KEY"),
    ):
        embed_code("00000000-0000-0000-0000-000000000001")
    engine.assert_not_called()


@pytest.mark.parametrize("network_error", [False, True])
def test_production_key_not_logged(network_error: bool, caplog: pytest.LogCaptureFixture) -> None:
    sentinel = "synthetic-audit-secret-never-real"
    caplog.set_level(logging.DEBUG)

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {sentinel}"
        if network_error:
            raise httpx.ConnectError(sentinel, request=request)
        return httpx.Response(401, text=sentinel)

    with httpx.Client(
        base_url="https://example.invalid/v1/",
        headers={"Authorization": f"Bearer {sentinel}"},
        transport=httpx.MockTransport(respond),
    ) as client:
        with pytest.raises(EmbeddingError) as error:
            OpenAIEmbeddingProvider(client, "fixture", 2, max_retries=0).embed(["test"])
    assert sentinel not in str(error.value)
    assert sentinel not in caplog.text
