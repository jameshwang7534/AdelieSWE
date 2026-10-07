"""Structured output, context isolation and redaction without external services."""

import asyncio
import io
import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.core.observability import JsonFormatter, bind, configure, context
from app.core.timing import observed
from app.main import create_app
from app.schemas.executions import ExecutionResponse
from app.workers.observability import finish, publish, start


@pytest.fixture
def output() -> Any:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("app")
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def test_redaction_and_no_payloads(output: io.StringIO) -> None:
    configure(
        Settings(
            github_token=SecretStr("fixture-token-value"),
            llm_api_key=SecretStr("fixture-llm-value"),
        )
    )
    with bind(repository_id=uuid4()):
        try:
            raise RuntimeError("exception contains fixture-llm-value")
        except RuntimeError:
            logging.getLogger("app.test").exception(
                "fixture-token-value Authorization: Bearer hidden-auth LLM_API_KEY=other-key",
                extra={"environment": {"secret": "env-value"}, "prompt": "private-code"},
            )
    line = output.getvalue()
    data = json.loads(line)
    assert data["repository_id"] and data["error_type"] == "RuntimeError"
    for forbidden in (
        "fixture-token-value",
        "fixture-llm-value",
        "hidden-auth",
        "other-key",
        "env-value",
        "private-code",
        "Traceback",
    ):
        assert forbidden not in line
    third_party = logging.LogRecord("httpx", logging.INFO, "", 0, "private payload", (), None)
    assert "private payload" not in JsonFormatter().format(third_party)


def test_requests_and_errors(output: io.StringIO) -> None:
    app = create_app(Settings(database_url=None, redis_url=None, celery_broker_url=None))

    @app.get("/observability-error")
    def broken() -> None:
        raise RuntimeError("private error body")

    identifier = str(uuid4())
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/health?token=private-query", headers={"X-Request-ID": identifier})
        assert (
            response.headers["X-Request-ID"] == response.headers["X-Correlation-ID"] == identifier
        )
        failure = client.get("/observability-error")
        assert failure.status_code == 500 and failure.headers["X-Request-ID"]
        invalid = client.get("/health", headers={"X-Request-ID": "unsafe-id"})
        assert invalid.headers["X-Request-ID"] != "unsafe-id"
    logs = [json.loads(line) for line in output.getvalue().splitlines()]
    requests = [r for r in logs if r["event"] == "http.request"]
    assert [r["status_code"] for r in requests] == [200, 500, 200]
    assert all(r["duration_ms"] >= 0 for r in requests)
    assert (
        "private-query" not in output.getvalue() and "private error body" not in output.getvalue()
    )
    assert not context.get()


@pytest.mark.asyncio
async def test_concurrent_async_context_and_exception_timing(output: io.StringIO) -> None:
    @observed("test.operation")
    async def operation(repository_id: str, fail: bool) -> None:
        await asyncio.sleep(0)
        if fail:
            raise ValueError("sensitive text")

    ids = [str(uuid4()), str(uuid4())]
    results = await asyncio.gather(
        operation(ids[0], False), operation(ids[1], True), return_exceptions=True
    )
    assert results[0] is None and isinstance(results[1], ValueError)
    logs = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [r["repository_id"] for r in logs] == ids
    assert [r["outcome"] for r in logs] == ["ok", "error"]
    assert not context.get() and "sensitive text" not in output.getvalue()


def test_celery_headers_and_reset(output: io.StringIO) -> None:
    identifier, task_id = str(uuid4()), str(uuid4())
    headers: dict[str, Any] = {}
    with bind(correlation_id=identifier, execution_id=uuid4()):
        publish(headers=headers)
    task = SimpleNamespace(request=SimpleNamespace(headers=headers))
    start(task, task_id)
    assert context.get()["correlation_id"] == identifier
    assert context.get()["celery_task_id"] == task_id
    finish(task, state="SUCCESS")
    assert not context.get()
    assert all(
        json.loads(line)["celery_task_id"] == task_id for line in output.getvalue().splitlines()
    )


def test_execution_elapsed_and_counts() -> None:
    started = datetime.now(UTC)
    response = ExecutionResponse(
        id=uuid4(),
        plan_id=uuid4(),
        status="completed",
        started_at=started,
        completed_at=started + timedelta(seconds=5),
        tasks=[],
    )
    assert response.model_dump()["elapsed_seconds"] == 5
    assert response.model_dump()["task_counts"] == {}
