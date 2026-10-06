"""Workflow API availability, explicit publication consent, and stage queue routing."""

from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest
from celery import Celery
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.main import create_app
from app.schemas.workflows import WorkflowRequest, WorkflowStatus
from app.services.workflow_resources import CeleryWorkflowQueue
from app.workers.factory import create_celery_app
from app.workers.workflow import ADVANCE_WORKFLOW, RECOVER_WORKFLOWS


def test_workflow_request_and_unconfigured_api() -> None:
    request = WorkflowRequest(
        request_id=uuid4(), github_owner="fixture", github_name="repo", issue_number=1
    )
    assert request.publish_pull_request is False
    with pytest.raises(ValidationError):
        WorkflowRequest.model_validate({**request.model_dump(), "publish_pull_request": "true"})
    with TestClient(create_app(Settings(database_url=None, celery_broker_url=None))) as client:
        assert client.post("/workflows", json=request.model_dump(mode="json")).status_code == 503
        assert client.get(f"/workflows/{request.request_id}").status_code == 503


@pytest.mark.parametrize(
    "stage,queue",
    [
        ("sync", "orchestration"),
        ("index", "indexing"),
        ("embed", "indexing"),
        ("implement", "agents"),
        ("plan", "agents"),
        ("review", "agents"),
        ("publish", "orchestration"),
    ],
)
def test_workflow_queue_routing(stage: str, queue: str) -> None:
    application = Mock(spec=Celery)
    status = WorkflowStatus(
        id=uuid4(),
        stage=stage,
        status="pending",
        attempts=0,
        error_code=None,
        history=[],
        updated_at=datetime.now(UTC),
    )
    CeleryWorkflowQueue(application).enqueue(status)
    application.send_task.assert_called_once_with(
        ADVANCE_WORKFLOW, args=[str(status.id), stage, status.generation], queue=queue
    )


def test_worker_configuration() -> None:
    application = create_celery_app(
        Settings(
            celery_broker_url=SecretStr("redis://localhost/1"),
            celery_result_backend=SecretStr("redis://localhost/2"),
        )
    )
    try:
        task = application.tasks[ADVANCE_WORKFLOW]
        assert task.acks_late and task.reject_on_worker_lost and task.time_limit == 960
        assert application.conf.beat_schedule["recover-workflows"]["task"] == RECOVER_WORKFLOWS
    finally:
        application.close()
