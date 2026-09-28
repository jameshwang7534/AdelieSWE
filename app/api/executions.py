"""Create durable runs and inspect state without executing repository code."""

import logging
from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from kombu.exceptions import OperationalError
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from app.orchestration.state import InvalidTransition
from app.schemas.executions import ExecutionResponse
from app.services.executions import ExecutionService, InvalidExecutionPlan
from app.services.repositories import RecordNotFound
from app.services.task_queue import TaskQueue

router = APIRouter(tags=["executions"])
logger = logging.getLogger(__name__)


def service_for(request: Request) -> ExecutionService:
    service = cast(ExecutionService | None, request.app.state.execution_service)
    if service is None:
        raise HTTPException(503, detail={"code": "database_unconfigured"})
    return service


@router.post("/plans/{plan_id}/executions", response_model=ExecutionResponse, status_code=202)
def create_execution(plan_id: UUID, request: Request) -> ExecutionResponse:
    service = service_for(request)
    queue = cast(TaskQueue | None, request.app.state.task_queue)
    if queue is None:
        raise HTTPException(503, detail={"code": "orchestration_unconfigured"})
    try:
        identifier = service.create(plan_id)
        result = service.get(identifier)
    except RecordNotFound:
        raise HTTPException(404, detail={"code": "record_not_found"}) from None
    except InvalidExecutionPlan:
        raise HTTPException(409, detail={"code": "invalid_execution_plan"}) from None
    except SQLAlchemyError:
        raise HTTPException(503, detail={"code": "database_unavailable"}) from None
    try:
        queue.enqueue_execution(identifier)
    except (OperationalError, RedisError, OSError):
        # The committed pending run is the durable work request. Beat recovers it.
        logger.warning(
            "Execution wake-up unavailable; persisted run awaits recovery id=%s", identifier
        )
    return result


@router.get("/executions/{execution_id}", response_model=ExecutionResponse)
def get_execution(execution_id: UUID, request: Request) -> ExecutionResponse:
    try:
        return service_for(request).get(execution_id)
    except RecordNotFound:
        raise HTTPException(404, detail={"code": "record_not_found"}) from None
    except (InvalidExecutionPlan, InvalidTransition):
        raise HTTPException(409, detail={"code": "execution_state_invalid"}) from None
    except SQLAlchemyError:
        raise HTTPException(503, detail={"code": "database_unavailable"}) from None
