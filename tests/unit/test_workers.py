"""Worker configuration, task behavior, and HTTP queue failures without Redis."""

import importlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.main import create_app
from app.schemas.tasks import PingMetadata, TaskState
from app.services.task_queue import CeleryTaskQueue, TaskQueue
from app.workers.factory import create_celery_app
from app.workers.tasks import PING_TASK_NAME, ping_payload


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name, raising=False)


def worker_settings() -> Settings:
    return Settings(
        celery_broker_url=SecretStr("redis://localhost:6379/1"),
        celery_result_backend=SecretStr("redis://localhost:6379/2"),
    )


def test_configuration_and_eager_task_without_redis() -> None:
    application = create_celery_app(worker_settings())
    try:
        assert {queue.name for queue in application.conf.task_queues} == {
            "orchestration",
            "indexing",
            "agents",
        }
        assert application.conf.task_routes[PING_TASK_NAME] == {"queue": "orchestration"}
        assert application.conf.accept_content == ["json"]
        assert application.conf.result_accept_content == ["json"]
        assert application.conf.result_backend_thread_safe is True
        assert application.conf.task_soft_time_limit < application.conf.task_time_limit
        assert application.conf.task_publish_retry_policy["max_retries"] == 2
        task = application.tasks[PING_TASK_NAME]
        assert task.max_retries == 3
        application.conf.update(task_always_eager=True, task_eager_propagates=True)
        result = task.apply(task_id=str(uuid4()))
        assert result.successful()
        metadata = PingMetadata.model_validate(result.result)
        assert str(metadata.task_id) == result.id
        json.dumps(result.result)
    finally:
        application.close()


def test_cli_import_does_not_create_db_or_network_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://localhost:6379/1")
    monkeypatch.setenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")
    with (
        patch("sqlalchemy.create_engine", side_effect=AssertionError("Unexpected database")),
        patch("socket.socket.connect", side_effect=AssertionError("Unexpected network")),
    ):
        module = importlib.import_module("app.workers.celery_app")
        module.app.close()


@pytest.mark.parametrize("name", ["CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"])
def test_worker_settings_reject_non_redis(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(name, "amqp://localhost")
    with pytest.raises(ValidationError):
        Settings()


def test_missing_configuration() -> None:
    with pytest.raises(ValueError, match="CELERY_BROKER_URL"):
        create_celery_app(Settings())
    with TestClient(create_app(Settings())) as client:
        assert client.post("/tasks/ping").status_code == 503
        assert client.get(f"/tasks/{uuid4()}").status_code == 503
        assert client.get("/health").status_code == 200


def test_queue_endpoints_and_cleanup() -> None:
    identifier = uuid4()
    queue = Mock(spec=TaskQueue)
    queue.enqueue_ping.return_value = identifier
    queue.inspect.return_value = TaskState(task_id=identifier, state="PENDING")
    stopped: list[bool] = []

    @contextmanager
    def resources(settings: Settings) -> Iterator[TaskQueue | None]:
        try:
            yield queue
        finally:
            stopped.append(True)

    with TestClient(create_app(Settings(), queue_factory=resources)) as client:
        response = client.post("/tasks/ping")
        assert response.status_code == 202
        assert response.json()["task_id"] == str(identifier)
        assert client.get(f"/tasks/{identifier}").json()["state"] == "PENDING"
        queue.inspect.assert_called_once_with(identifier)
        assert client.get("/tasks/not-a-uuid").status_code == 422
    assert stopped == [True]


@pytest.mark.parametrize("operation", ["submit", "inspect"])
def test_backend_failures_are_sanitized(operation: str, caplog: pytest.LogCaptureFixture) -> None:
    queue = Mock(spec=TaskQueue)
    queue.enqueue_ping.side_effect = ConnectionError("synthetic-sensitive-detail")
    queue.inspect.side_effect = ConnectionError("synthetic-sensitive-detail")

    @contextmanager
    def resources(settings: Settings) -> Iterator[TaskQueue | None]:
        yield queue

    with TestClient(create_app(Settings(), queue_factory=resources)) as client:
        response = (
            client.post("/tasks/ping") if operation == "submit" else client.get(f"/tasks/{uuid4()}")
        )
        assert response.status_code == 503
        assert "synthetic-sensitive-detail" not in response.text + caplog.text
        assert client.get("/health").status_code == 200


def test_adapter_state_does_not_expose_failure_details() -> None:
    application = Mock()
    identifier = uuid4()
    application.backend.get_task_meta.return_value = {
        "status": "FAILURE",
        "result": RuntimeError("synthetic-sensitive-detail"),
    }
    queue = CeleryTaskQueue(application)
    state = queue.inspect(identifier)
    assert state.state == "FAILURE" and state.result is None
    application.backend.get_task_meta.return_value = {
        "status": "SUCCESS",
        "result": ping_payload(str(identifier), "test", "orchestration", 0),
    }
    assert queue.inspect(identifier).result is not None
    application.send_task.return_value.id = str(identifier)
    assert queue.enqueue_ping() == identifier
    application.send_task.assert_called_once_with(PING_TASK_NAME)


def test_ping_payload_is_structured() -> None:
    identifier = uuid4()
    result = PingMetadata.model_validate(
        ping_payload(str(identifier), "worker", "orchestration", 2)
    )
    assert isinstance(result.task_id, UUID)
    assert result.retries == 2 and result.completed_at.utcoffset() is not None


def test_transient_task_failure_retries_without_a_broker() -> None:
    application = create_celery_app(worker_settings())
    identifier = str(uuid4())
    expected = ping_payload(identifier, "worker", "orchestration", 1)
    try:
        with patch(
            "app.workers.tasks.ping_payload", side_effect=[TimeoutError("test"), expected]
        ) as payload:
            result = application.tasks[PING_TASK_NAME].apply(task_id=identifier)
            assert result.successful() and result.result == expected
            assert payload.call_count == 2
            assert payload.call_args.args[-1] == 1
    finally:
        application.close()
