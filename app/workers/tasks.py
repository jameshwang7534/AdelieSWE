"""Only the diagnostic task is implemented; no database or agent imports."""

from datetime import UTC, datetime
from uuid import UUID

from celery import Task
from celery.utils.log import get_task_logger

from app.schemas.tasks import PingMetadata

PING_TASK_NAME = "system.ping"
logger = get_task_logger(__name__)


def ping_payload(task_id: str, worker: str, queue: str, retries: int) -> dict[str, object]:
    """Pure diagnostic behavior, usable without a broker or a worker."""
    return PingMetadata(
        task_id=UUID(task_id),
        worker=worker,
        queue=queue,
        retries=retries,
        completed_at=datetime.now(UTC),
    ).model_dump(mode="json")


def ping(task: Task) -> dict[str, object]:
    request = task.request
    result = ping_payload(
        str(request.id),
        str(request.hostname or "unknown"),
        str((request.delivery_info or {}).get("routing_key", "orchestration")),
        int(request.retries),
    )
    logger.info("Diagnostic task completed task_id=%s", request.id)
    return result
