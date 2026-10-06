"""One attempt per standalone delivery; uncertain effects are inspected, never replayed."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope
from app.integrations.llm.embeddings import EmbeddingUnconfigured
from app.models.delivery import WorkerDelivery
from app.orchestration.state import InvalidTransition
from app.services.repositories import RecordNotFound


class DeliveryRecords:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def get(self, identifier: UUID) -> dict[str, object]:
        with session_scope(self.sessions) as session:
            record = session.get(WorkerDelivery, identifier)
            if record is None:
                raise RecordNotFound
            return {
                "id": str(record.id),
                "task_name": record.task_name,
                "repository_id": str(record.repository_id),
                "status": record.status,
                "error_code": record.error_code,
                "result": record.result,
            }

    def execute(
        self,
        identifier: UUID,
        task_name: str,
        repository_id: UUID,
        operation: Callable[[], dict[str, object]],
    ) -> dict[str, object]:
        with session_scope(self.sessions) as session:
            created = session.scalar(
                insert(WorkerDelivery)
                .values(
                    id=identifier,
                    task_name=task_name,
                    repository_id=repository_id,
                    status="running",
                    result={},
                    lease_until=datetime.now(UTC) + timedelta(seconds=1800),
                )
                .on_conflict_do_nothing(index_elements=[WorkerDelivery.id])
                .returning(WorkerDelivery.id)
            )
            if created is None:
                record = session.get(WorkerDelivery, identifier)
                assert record is not None
                if record.task_name != task_name or record.repository_id != repository_id:
                    raise InvalidTransition("delivery_identity_conflict")
                if record.status == "completed":
                    return dict(record.result)
                if record.status == "failed":
                    raise InvalidTransition("delivery_failed_requires_inspection")
                return {"repository_id": str(repository_id), "status": "running", "duplicate": True}
        try:
            result = operation()
        except EmbeddingUnconfigured:
            self.finish(identifier, {}, "embedding_unconfigured")
            raise EmbeddingUnconfigured(
                "embedding_unconfigured: set LLM_API_KEY and EMBEDDING_MODEL"
            ) from None
        except Exception:
            self.finish(identifier, {}, "repository_delivery_failed")
            raise RuntimeError("repository_delivery_failed") from None
        self.finish(identifier, result, None)
        return result

    def finish(self, identifier: UUID, result: dict[str, object], error: str | None) -> None:
        with session_scope(self.sessions) as session:
            record = session.scalar(
                select(WorkerDelivery).where(WorkerDelivery.id == identifier).with_for_update()
            )
            if record is None or record.status != "running":
                raise InvalidTransition("delivery_claim_lost")
            record.status = "failed" if error else "completed"
            record.result, record.error_code = result, error

    def recover_stale(self) -> int:
        with session_scope(self.sessions) as session:
            records = session.scalars(
                select(WorkerDelivery)
                .where(
                    WorkerDelivery.status == "running",
                    WorkerDelivery.lease_until < datetime.now(UTC),
                )
                .limit(100)
                .with_for_update(skip_locked=True)
            ).all()
            for record in records:
                record.status, record.error_code = "failed", "worker_interrupted"
            return len(records)
