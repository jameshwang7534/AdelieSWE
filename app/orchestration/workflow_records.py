"""Short DB transactions own claims/checkpoints; the pending row is the durable outbox."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.session import session_scope
from app.models import WorkflowRun
from app.orchestration.transitions import workflow_state
from app.schemas.workflows import WorkflowRequest, WorkflowStatus
from app.services.executions import ExecutionService
from app.services.repositories import RecordNotFound

STAGES = (
    "repository",
    "sync",
    "index",
    "embed",
    "issue",
    "context",
    "plan",
    "execution",
    "workspace",
    "implement",
    "review",
    "publish",
    "done",
)


class WorkflowError(Exception):
    """Fixed public error codes only."""


@dataclass(frozen=True)
class WorkflowClaim:
    id: UUID
    token: UUID
    stage: str
    request: WorkflowRequest
    data: dict[str, object]


class WorkflowRecords:
    def __init__(self, sessions: sessionmaker[Session], settings: Settings) -> None:
        self.sessions, self.settings = sessions, settings

    def start(self, request: WorkflowRequest) -> WorkflowStatus:
        payload = request.model_dump(mode="json")
        with session_scope(self.sessions) as session:
            session.execute(
                insert(WorkflowRun)
                .values(
                    id=request.request_id,
                    request=payload,
                    data={"required_commands": list(self.settings.workflow_required_tests)},
                    history=[],
                    stage="repository",
                    status="pending",
                    attempts=0,
                )
                .on_conflict_do_nothing(index_elements=[WorkflowRun.id])
            )
            record = session.get(WorkflowRun, request.request_id)
            if record is None or record.request != payload:
                raise WorkflowError("workflow_idempotency_conflict")
        return self.get(request.request_id)

    def get(self, identifier: UUID) -> WorkflowStatus:
        with session_scope(self.sessions) as session:
            record = session.get(WorkflowRun, identifier)
            if record is None:
                raise RecordNotFound
            return WorkflowStatus.model_validate(
                {
                    "id": record.id,
                    "stage": record.stage,
                    "status": record.status,
                    "attempts": record.attempts,
                    "generation": record.generation,
                    "retry_at": record.retry_at,
                    "error_code": record.error_code,
                    "history": record.history,
                    "updated_at": record.updated_at,
                    **{
                        key: value
                        for key, value in record.data.items()
                        if key
                        in {
                            "repository_id",
                            "issue_id",
                            "plan_id",
                            "execution_id",
                            "pull_request_url",
                        }
                    },
                }
            )

    def claim(
        self,
        identifier: UUID,
        expected_stage: str | None = None,
        expected_generation: int | None = None,
    ) -> WorkflowClaim | None:
        with session_scope(self.sessions) as session:
            record = session.scalar(
                select(WorkflowRun).where(WorkflowRun.id == identifier).with_for_update()
            )
            if record is None:
                raise RecordNotFound
            if (
                record.status != "pending"
                or (expected_stage is not None and record.stage != expected_stage)
                or (expected_generation is not None and record.generation != expected_generation)
                or (record.retry_at is not None and record.retry_at > datetime.now(UTC))
            ):
                return None
            workflow_state(record, "running")
            record.claim_token = uuid4()
            record.retry_at = None
            record.attempts += 1
            record.lease_until = datetime.now(UTC) + timedelta(seconds=1800)
            record.history = [
                *record.history,
                {"stage": record.stage, "status": "running", "at": datetime.now(UTC).isoformat()},
            ]
            return WorkflowClaim(
                record.id,
                record.claim_token,
                record.stage,
                WorkflowRequest.model_validate(record.request),
                dict(record.data),
            )

    def finish(
        self,
        claim: WorkflowClaim,
        data: dict[str, object],
        *,
        repeat: bool = False,
        error: str | None = None,
        blocked: bool = False,
        retry_seconds: int | None = None,
    ) -> None:
        with session_scope(self.sessions) as session:
            record = session.scalar(
                select(WorkflowRun).where(WorkflowRun.id == claim.id).with_for_update()
            )
            if record is None or record.claim_token != claim.token or record.status != "running":
                raise WorkflowError("workflow_claim_lost")
            record.history = [
                *record.history,
                {
                    "stage": claim.stage,
                    "status": "failed" if error else "completed",
                    "error_code": error,
                    "at": datetime.now(UTC).isoformat(),
                },
            ]
            record.data = {**record.data, **data}
            record.error_code, record.claim_token, record.lease_until = error, None, None
            record.generation += 1
            if error:
                if (
                    not blocked
                    and retry_seconds is not None
                    and record.attempts < self.settings.workflow_stage_attempts
                ):
                    workflow_state(record, "pending")
                    record.retry_at = datetime.now(UTC) + timedelta(seconds=retry_seconds)
                else:
                    workflow_state(record, "blocked" if blocked else "failed")
            else:
                record.stage = claim.stage if repeat else STAGES[STAGES.index(claim.stage) + 1]
                if record.stage == "publish" and not claim.request.publish_pull_request:
                    record.stage = "done"
                workflow_state(record, "completed" if record.stage == "done" else "pending")
                record.attempts = 0

    def resume(self, identifier: UUID) -> WorkflowStatus:
        with session_scope(self.sessions) as session:
            record = session.scalar(
                select(WorkflowRun).where(WorkflowRun.id == identifier).with_for_update()
            )
            if record is None:
                raise RecordNotFound
            if (
                record.status == "failed"
                and record.attempts < self.settings.workflow_stage_attempts
            ):
                workflow_state(record, "pending")
                record.error_code, record.retry_at = None, None
                record.generation += 1
            elif record.status != "pending":
                raise WorkflowError("workflow_resume_not_allowed")
        return self.get(identifier)

    def cancel(self, identifier: UUID) -> WorkflowStatus:
        with session_scope(self.sessions) as session:
            record = session.scalar(
                select(WorkflowRun).where(WorkflowRun.id == identifier).with_for_update()
            )
            if record is None:
                raise RecordNotFound
            if record.status not in {"pending", "failed", "blocked", "cancelled"}:
                raise WorkflowError("workflow_cancel_not_allowed")
            if record.status != "cancelled":
                execution_id = record.data.get("execution_id")
                if isinstance(execution_id, str):
                    ExecutionService(self.sessions).stop_locked(
                        session, UUID(execution_id), cancelled=True
                    )
                workflow_state(record, "cancelled")
                record.generation += 1
                record.retry_at = None
                record.error_code = "workflow_cancelled"
                record.history = [
                    *record.history,
                    {
                        "stage": record.stage,
                        "status": "cancelled",
                        "at": datetime.now(UTC).isoformat(),
                        "error_code": record.error_code,
                    },
                ]
        return self.get(identifier)

    def recoverable(self) -> list[UUID]:
        with session_scope(self.sessions) as session:
            expired = session.scalars(
                select(WorkflowRun)
                .where(
                    WorkflowRun.status == "running",
                    WorkflowRun.lease_until < datetime.now(UTC),
                )
                .limit(100)
                .with_for_update(skip_locked=True)
            ).all()
            for record in expired:
                # Fence the old claim; never replay possibly applied source patches automatically.
                workflow_state(
                    record, "blocked" if record.stage in {"implement", "review"} else "failed"
                )
                execution_id = record.data.get("execution_id")
                if record.stage in {"implement", "review"} and isinstance(execution_id, str):
                    ExecutionService(self.sessions).stop_locked(
                        session, UUID(execution_id), cancelled=False
                    )
                record.generation += 1
                record.error_code = "workflow_worker_interrupted"
                record.claim_token, record.lease_until = None, None
                record.history = [
                    *record.history,
                    {
                        "stage": record.stage,
                        "status": record.status,
                        "error_code": "workflow_worker_interrupted",
                        "at": datetime.now(UTC).isoformat(),
                    },
                ]
            return list(
                session.scalars(
                    select(WorkflowRun.id)
                    .where(WorkflowRun.status == "pending")
                    .where(
                        or_(
                            WorkflowRun.retry_at.is_(None),
                            WorkflowRun.retry_at <= datetime.now(UTC),
                        )
                    )
                    .order_by(WorkflowRun.updated_at)
                    .limit(100)
                )
            )
