"""Opt-in real Redis transport and Celery worker test (not eager execution)."""

import os
import time
from uuid import UUID, uuid4

import pytest
from celery.contrib.testing.worker import start_worker
from fastapi.testclient import TestClient
from kombu import Queue

from app.core.config import Settings
from app.main import create_app
from app.workers.factory import create_celery_app
from app.workers.tasks import PING_TASK_NAME

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_WORKER_TESTS") != "1",
    reason="Set RUN_WORKER_TESTS=1 and configure Celery Redis URLs for the live worker test.",
)


def test_ping_through_real_worker_and_api() -> None:
    settings = Settings()
    worker_app = create_celery_app(settings)
    # A dedicated queue prevents the test worker from consuming unrelated development work.
    queue_name = f"diagnostic-test-{uuid4().hex}"
    worker_app.conf.task_queues = (Queue(queue_name),)
    worker_app.conf.task_default_queue = queue_name
    worker_app.conf.task_routes = {PING_TASK_NAME: {"queue": queue_name}}
    identifier: UUID | None = None
    try:
        with start_worker(worker_app, pool="solo", perform_ping_check=False, queues=[queue_name]):
            api = create_app(settings)
            with TestClient(api) as client:
                gateway = api.state.task_queue
                gateway.application.conf.task_queues = (Queue(queue_name),)
                gateway.application.conf.task_default_queue = queue_name
                gateway.application.conf.task_routes = {PING_TASK_NAME: {"queue": queue_name}}
                response = client.post("/tasks/ping")
                assert response.status_code == 202
                identifier = UUID(response.json()["task_id"])
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    state = client.get(f"/tasks/{identifier}")
                    assert state.status_code == 200
                    if state.json()["state"] == "SUCCESS":
                        assert state.json()["result"]["task_name"] == PING_TASK_NAME
                        assert state.json()["result"]["queue"] == queue_name
                        assert state.json()["result"]["task_id"] == str(identifier)
                        break
                    assert state.json()["state"] not in {"FAILURE", "REVOKED"}
                    time.sleep(0.1)
                else:
                    pytest.fail("Diagnostic task did not finish within 20 seconds")
    finally:
        try:
            if identifier is not None:
                worker_app.backend.forget(str(identifier))
            with worker_app.connection_for_write() as connection:
                Queue(queue_name)(connection.default_channel).delete(if_unused=True, if_empty=True)
        finally:
            worker_app.backend.result_consumer.stop()
            worker_app.backend.client.close()
            worker_app.close()
