"""API workflow records/queue with injectable lifecycle; no provider calls at startup."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

from celery import Celery

from app.core.config import Settings
from app.db.session import create_database_engine, create_session_factory
from app.orchestration.workflow_records import WorkflowRecords
from app.schemas.workflows import WorkflowStatus
from app.workers.workflow import ADVANCE_WORKFLOW


class WorkflowQueue(Protocol):
    def enqueue(self, workflow: WorkflowStatus) -> None: ...


class CeleryWorkflowQueue:
    def __init__(self, application: Celery) -> None:
        self.application = application

    def enqueue(self, workflow: WorkflowStatus) -> None:
        queue = (
            "indexing"
            if workflow.stage in {"index", "embed"}
            else "agents"
            if workflow.stage in {"plan", "implement", "review"}
            else "orchestration"
        )
        self.application.send_task(
            ADVANCE_WORKFLOW,
            args=[str(workflow.id), workflow.stage, workflow.generation],
            queue=queue,
        )


@contextmanager
def workflow_api_resources(
    settings: Settings,
) -> Iterator[tuple[WorkflowRecords, WorkflowQueue] | None]:
    from app.workers.factory import create_celery_app

    if (
        not settings.database_url
        or not settings.celery_broker_url
        or not settings.celery_result_backend
    ):
        yield None
        return
    engine = create_database_engine(settings)
    application = create_celery_app(settings)
    try:
        yield (
            WorkflowRecords(create_session_factory(engine), settings),
            CeleryWorkflowQueue(application),
        )
    finally:
        application.close()
        engine.dispose()
