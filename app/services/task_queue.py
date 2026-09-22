"""Replaceable queue boundary used by the diagnostic HTTP endpoints."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol
from uuid import UUID

from celery import Celery

from app.core.config import Settings
from app.schemas.tasks import PingMetadata, TaskState
from app.workers.factory import create_celery_app
from app.workers.tasks import PING_TASK_NAME


class TaskQueue(Protocol):
    def enqueue_ping(self) -> UUID: ...
    def inspect(self, task_id: UUID) -> TaskState: ...


class CeleryTaskQueue:
    def __init__(self, application: Celery) -> None:
        self.application = application

    def enqueue_ping(self) -> UUID:
        result = self.application.send_task(PING_TASK_NAME)
        return UUID(str(result.id))

    def inspect(self, task_id: UUID) -> TaskState:
        metadata = self.application.backend.get_task_meta(str(task_id))
        state = str(metadata["status"])
        result = None
        if state == "SUCCESS":
            result = PingMetadata.model_validate(metadata["result"])
            if result.task_id != task_id:
                raise ValueError("Mismatched diagnostic result")
        return TaskState(task_id=task_id, state=state, result=result)


@contextmanager
def task_queue_resources(settings: Settings) -> Iterator[TaskQueue | None]:
    if settings.celery_broker_url is None or settings.celery_result_backend is None:
        yield None
        return
    application = create_celery_app(settings)
    try:
        yield CeleryTaskQueue(application)
    finally:
        try:
            application.backend.result_consumer.stop()
            application.backend.client.close()
        finally:
            application.close()
