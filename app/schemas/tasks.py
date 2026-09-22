"""Public diagnostic task contracts; no raw task exceptions or arbitrary results."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class PingMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: UUID
    task_name: Literal["system.ping"] = "system.ping"
    status: Literal["ok"] = "ok"
    worker: str
    queue: str
    retries: int
    completed_at: datetime


class EnqueuedTask(BaseModel):
    task_id: UUID
    task_name: Literal["system.ping"] = "system.ping"
    status: Literal["queued"] = "queued"


class TaskState(BaseModel):
    task_id: UUID
    state: str
    result: PingMetadata | None = None
