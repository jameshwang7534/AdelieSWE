"""Local diagnostic task submission and nonblocking result inspection."""

import logging
from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from app.schemas.tasks import EnqueuedTask, TaskState
from app.services.task_queue import TaskQueue

router = APIRouter(prefix="/tasks", tags=["diagnostic tasks"])
logger = logging.getLogger("app.tasks")


def get_queue(request: Request) -> TaskQueue:
    queue = cast(TaskQueue | None, request.app.state.task_queue)
    if queue is None:
        raise HTTPException(status_code=503, detail="Task queue is not configured")
    return queue


@router.post("/ping", status_code=202, response_model=EnqueuedTask)
def enqueue_ping(request: Request) -> EnqueuedTask:
    queue = get_queue(request)
    try:
        return EnqueuedTask(task_id=queue.enqueue_ping())
    except Exception:
        logger.warning("Diagnostic task submission failed")
        raise HTTPException(status_code=503, detail="Task queue is unavailable") from None


@router.get("/{task_id}", response_model=TaskState)
def inspect_task(task_id: UUID, request: Request) -> TaskState:
    queue = get_queue(request)
    try:
        return queue.inspect(task_id)
    except Exception:
        logger.warning("Diagnostic task inspection failed")
        raise HTTPException(status_code=503, detail="Task result backend is unavailable") from None
