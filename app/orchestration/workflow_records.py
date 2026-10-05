"""Short DB transactions own claims/checkpoints; the pending row is the durable outbox."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.session import session_scope
from app.models import WorkflowRun
from app.schemas.workflows import WorkflowRequest, WorkflowStatus
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

    def claim(self, identifier: UUID, expected_stage: str | None = None) -> WorkflowClaim | None:
        with session_scope(self.sessions) as session:
            record = session.scalar(
                select(WorkflowRun).where(WorkflowRun.id == identifier).with_for_update()
            )
            if record is None:
                raise RecordNotFound
            if record.status != "pending" or (
                expected_stage is not None and record.stage != expected_stage
            ):
                return None
            record.status, record.claim_token = "running", uuid4()
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
            if error:
                record.status = "blocked" if blocked else "failed"
            else:
                record.stage = claim.stage if repeat else STAGES[STAGES.index(claim.stage) + 1]
                if record.stage == "publish" and not claim.request.publish_pull_request:
                    record.stage = "done"
                record.status = "completed" if record.stage == "done" else "pending"
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
                record.status, record.error_code = "pending", None
            elif record.status != "pending":
                raise WorkflowError("workflow_resume_not_allowed")
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
                record.status = "blocked" if record.stage in {"implement", "review"} else "failed"
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
                    .order_by(WorkflowRun.updated_at)
                    .limit(100)
                )
            )
