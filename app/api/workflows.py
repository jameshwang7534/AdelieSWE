"""Start an idempotent workflow, poll persisted progress, or resume a retryable stage."""

import logging
from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from app.orchestration.deliveries import DeliveryRecords
from app.orchestration.state import InvalidTransition
from app.orchestration.workflow_records import WorkflowError, WorkflowRecords
from app.schemas.workflows import WorkflowRequest, WorkflowStatus
from app.services.repositories import RecordNotFound
from app.services.workflow_resources import WorkflowQueue

router = APIRouter(tags=["workflows"])
logger = logging.getLogger(__name__)


def resources(request: Request) -> tuple[WorkflowRecords, WorkflowQueue]:
    value = cast(tuple[WorkflowRecords, WorkflowQueue] | None, request.app.state.workflow_resources)
    if value is None:
        raise HTTPException(503, detail={"code": "workflow_unconfigured"})
    return value


def wake(queue: WorkflowQueue, status: WorkflowStatus) -> WorkflowStatus:
    if status.status == "pending" and status.retry_at is None:
        try:
            queue.enqueue(status)
        except Exception:
            logger.warning("Workflow wake-up unavailable; pending row retained id=%s", status.id)
    return status


@router.post("/workflows", response_model=WorkflowStatus, status_code=202)
def start_workflow(body: WorkflowRequest, request: Request) -> WorkflowStatus:
    records, queue = resources(request)
    try:
        return wake(queue, records.start(body))
    except WorkflowError:
        raise HTTPException(409, detail={"code": "workflow_idempotency_conflict"}) from None
    except SQLAlchemyError:
        raise HTTPException(503, detail={"code": "database_unavailable"}) from None


@router.get("/workflows/{workflow_id}", response_model=WorkflowStatus)
def workflow_status(workflow_id: UUID, request: Request) -> WorkflowStatus:
    records, _ = resources(request)
    try:
        return records.get(workflow_id)
    except RecordNotFound:
        raise HTTPException(404, detail={"code": "workflow_not_found"}) from None
    except SQLAlchemyError:
        raise HTTPException(503, detail={"code": "database_unavailable"}) from None


@router.post("/workflows/{workflow_id}/resume", response_model=WorkflowStatus, status_code=202)
def resume_workflow(workflow_id: UUID, request: Request) -> WorkflowStatus:
    records, queue = resources(request)
    try:
        return wake(queue, records.resume(workflow_id))
    except RecordNotFound:
        raise HTTPException(404, detail={"code": "workflow_not_found"}) from None
    except WorkflowError:
        raise HTTPException(409, detail={"code": "workflow_resume_not_allowed"}) from None
    except SQLAlchemyError:
        raise HTTPException(503, detail={"code": "database_unavailable"}) from None


@router.post("/workflows/{workflow_id}/cancel", response_model=WorkflowStatus)
def cancel_workflow(workflow_id: UUID, request: Request) -> WorkflowStatus:
    records, _ = resources(request)
    try:
        return records.cancel(workflow_id)
    except RecordNotFound:
        raise HTTPException(404, detail={"code": "workflow_not_found"}) from None
    except (WorkflowError, InvalidTransition):
        raise HTTPException(409, detail={"code": "workflow_cancel_not_allowed"}) from None
    except SQLAlchemyError:
        raise HTTPException(503, detail={"code": "database_unavailable"}) from None


@router.get("/worker-deliveries/{delivery_id}")
def delivery_status(delivery_id: UUID, request: Request) -> dict[str, object]:
    records, _ = resources(request)
    try:
        return DeliveryRecords(records.sessions).get(delivery_id)
    except RecordNotFound:
        raise HTTPException(404, detail={"code": "delivery_not_found"}) from None
    except SQLAlchemyError:
        raise HTTPException(503, detail={"code": "database_unavailable"}) from None
