"""Durable retry, fencing, cancellation and interrupted-worker regression tests."""

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.session import session_scope
from app.integrations.github.client import GitHubError
from app.integrations.llm.provider import LLMError
from app.main import create_app
from app.models import AgentRun, ImplementationPlan, Issue, WorkerDelivery, WorkflowRun
from app.orchestration.deliveries import DeliveryRecords
from app.orchestration.state import InvalidTransition
from app.orchestration.workflow import WorkflowEngine
from app.orchestration.workflow_records import WorkflowError, WorkflowRecords
from app.orchestration.workflow_stages import StageResult
from app.schemas.workflows import WorkflowRequest
from app.services.executions import ExecutionService
from app.services.workflow_resources import WorkflowQueue
from tests.integration.test_database import engine as engine
from tests.integration.test_database import factory as factory
from tests.integration.test_executions import make_plan

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Requires PostgreSQL"
)


def request() -> WorkflowRequest:
    return WorkflowRequest(
        request_id=uuid4(), github_owner="fixture", github_name="repo", issue_number=1
    )


def test_generation_fences_repeated_stage_and_cancel(factory: sessionmaker[Session]) -> None:
    records = WorkflowRecords(factory, Settings())
    status = records.start(request())
    claim = records.claim(status.id, status.stage, status.generation)
    assert claim is not None
    records.finish(claim, {}, repeat=True)
    assert records.claim(status.id, status.stage, status.generation) is None
    next_status = records.get(status.id)
    assert next_status.generation == 1
    cancelled = records.cancel(status.id)
    assert cancelled.status == "cancelled" and cancelled.generation == 2
    assert records.cancel(status.id).generation == 2
    assert records.claim(status.id) is None
    with pytest.raises(WorkflowError):
        records.resume(status.id)


def test_concurrent_claims_and_restart(engine: Engine) -> None:
    sessions = sessionmaker(engine, expire_on_commit=False)
    status = WorkflowRecords(sessions, Settings()).start(request())

    def claim(_: int) -> bool:
        return WorkflowRecords(sessions, Settings()).claim(status.id) is not None

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, range(2))) == [False, True]
    restarted = WorkflowRecords(sessions, Settings())
    assert restarted.get(status.id).attempts == 1
    with pytest.raises(WorkflowError, match="cancel_not_allowed"):
        restarted.cancel(status.id)


def test_stale_standalone_execution(factory: sessionmaker[Session]) -> None:
    service = ExecutionService(factory)
    identifier = service.create(make_plan(factory))
    task = service.reconcile(identifier).tasks[0]
    service.transition(identifier, task.id, "running")
    assert service.recover_stale(3600) == 0
    assert service.recover_stale(0) == 1
    assert service.reconcile(identifier).status == "failed"
    assert service.get(identifier).tasks[0].output_summary == "worker_interrupted"
    assert service.recover_stale(0) == 0


def test_broker_reconnect_and_cancellation_api(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.services import workflow_resources
    from app.workers.workflow import recover_workflows

    records = WorkflowRecords(factory, Settings())
    first, second = records.start(request()), records.start(request())
    queue = Mock()
    queue.enqueue.side_effect = [ConnectionError("private broker detail"), None]

    @contextmanager
    def resources(settings: Settings) -> Iterator[tuple[WorkflowRecords, WorkflowQueue] | None]:
        yield records, queue

    monkeypatch.setattr(workflow_resources, "workflow_api_resources", resources)
    assert recover_workflows() == {"dispatched": 1}
    assert "private broker detail" not in caplog.text
    queue.enqueue.side_effect = None
    assert recover_workflows() == {"dispatched": 2}
    assert records.get(first.id).status == records.get(second.id).status == "pending"
    with TestClient(create_app(Settings(database_url=None), workflow_factory=resources)) as client:
        for _ in range(2):
            response = client.post(f"/workflows/{first.id}/cancel")
            assert response.status_code == 200 and response.json()["status"] == "cancelled"
        assert client.post(f"/workflows/{first.id}/resume").status_code == 409
        assert client.post(f"/workflows/{uuid4()}/cancel").status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [GitHubError("github_unavailable", 503), LLMError("llm_timeout")]
)
async def test_bounded_delayed_retry(factory: sessionmaker[Session], failure: Exception) -> None:
    records = WorkflowRecords(
        factory, Settings(workflow_stage_attempts=2, workflow_retry_seconds=1)
    )
    status = records.start(request())

    class Fail:
        async def run(self, claim: object) -> StageResult:
            raise failure

    engine = WorkflowEngine(records, Fail())
    status = await engine.advance(status.id)
    assert status.status == "pending" and status.retry_at is not None and status.attempts == 1
    assert records.claim(status.id) is None and status.id not in records.recoverable()
    with session_scope(factory) as session:
        row = session.get(WorkflowRun, status.id)
        assert row
        row.retry_at = datetime.now(UTC) - timedelta(seconds=1)
    assert status.id in records.recoverable()
    status = await engine.advance(status.id)
    assert status.status == "failed" and status.attempts == 2
    assert status.error_code in {"github_unavailable", "llm_timeout"}
    with pytest.raises(WorkflowError):
        records.resume(status.id)


def test_claim_transaction_rollback(factory: sessionmaker[Session]) -> None:
    records = WorkflowRecords(factory, Settings())
    status = records.start(request())

    def fail_commit(session: Session) -> None:
        raise RuntimeError("simulated transaction failure")

    event.listen(Session, "before_commit", fail_commit)
    try:
        with pytest.raises(RuntimeError):
            records.claim(status.id)
    finally:
        event.remove(Session, "before_commit", fail_commit)
    assert records.get(status.id).attempts == 0
    assert records.get(status.id).status == "pending"
    assert records.claim(status.id) is not None


@pytest.mark.asyncio
async def test_checkpoint_failure_does_not_replay_effects(factory: sessionmaker[Session]) -> None:
    records = WorkflowRecords(factory, Settings())
    status = records.start(request())
    calls: list[str] = []

    def fail_commit(session: Session) -> None:
        raise RuntimeError("commit failed after effect")

    class Effect:
        async def run(self, claim: object) -> StageResult:
            calls.append("external effect")
            event.listen(Session, "before_commit", fail_commit)
            return StageResult()

    try:
        with pytest.raises(RuntimeError, match="commit failed"):
            await WorkflowEngine(records, Effect()).advance(status.id)
    finally:
        event.remove(Session, "before_commit", fail_commit)
    assert records.get(status.id).status == "running"
    await WorkflowEngine(WorkflowRecords(factory, Settings()), Effect()).advance(status.id)
    assert calls == ["external effect"]


def test_execution_cancel_and_stale_workflow(factory: sessionmaker[Session]) -> None:
    executions = ExecutionService(factory)
    run_id = executions.create(make_plan(factory, diamond=True))
    tasks = executions.reconcile(run_id).tasks
    assert executions.transition(run_id, tasks[0].id, "running")
    with pytest.raises(InvalidTransition, match="execution_busy"):
        executions.cancel(run_id)
    with session_scope(factory) as session:
        session.add(
            AgentRun(
                execution_run_id=run_id,
                task_execution_id=tasks[0].id,
                agent_type="coding",
                status="running",
                started_at=datetime.now(UTC),
            )
        )
    records = WorkflowRecords(factory, Settings())
    status = records.start(request())
    claim = records.claim(status.id)
    assert claim
    with session_scope(factory) as session:
        row = session.get(WorkflowRun, status.id)
        assert row
        row.stage, row.data = "implement", {"execution_id": str(run_id)}
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    records.recoverable()
    assert records.get(status.id).status == "blocked"
    assert executions.get(run_id).status == "failed"
    assert [t.status for t in executions.get(run_id).tasks] == [
        "failed",
        "blocked",
        "blocked",
        "blocked",
    ]
    assert executions.reconcile(run_id).status == "failed"
    with session_scope(factory) as session:
        agent = session.scalar(select(AgentRun).where(AgentRun.execution_run_id == run_id))
        assert agent and agent.status == "failed"
        assert agent.output_metadata["error_code"] == "worker_interrupted"
    with pytest.raises(WorkflowError, match="claim_lost"):
        records.finish(claim, {})
    other = executions.create(make_plan(factory))
    assert executions.cancel(other).status == "cancelled"
    assert executions.reconcile(other).status == "cancelled"
    assert all(t.status == "cancelled" for t in executions.get(other).tasks)


def test_standalone_delivery_deduplication_and_crash(factory: sessionmaker[Session]) -> None:
    plan_id = make_plan(factory)
    with session_scope(factory) as session:
        plan = session.get(ImplementationPlan, plan_id)
        assert plan
        issue = session.get(Issue, plan.issue_id)
        assert issue
        repository_id = issue.repository_id
    records = DeliveryRecords(factory)
    identifier = uuid4()
    operation = Mock(return_value={"status": "ready", "embedded": 1})
    for _ in range(2):
        assert records.execute(identifier, "embed", repository_id, operation)["embedded"] == 1
    operation.assert_called_once()
    broken_id = uuid4()
    with pytest.raises(RuntimeError, match="repository_delivery_failed"):
        records.execute(
            broken_id, "embed", repository_id, Mock(side_effect=TimeoutError("private"))
        )
    assert records.get(broken_id)["error_code"] == "repository_delivery_failed"
    with pytest.raises(InvalidTransition):
        records.execute(broken_id, "embed", repository_id, operation)
    stale_id = uuid4()
    with session_scope(factory) as session:
        session.add(
            WorkerDelivery(
                id=stale_id,
                repository_id=repository_id,
                task_name="embed",
                status="running",
                result={},
                lease_until=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
    assert records.execute(stale_id, "embed", repository_id, operation)["duplicate"] is True
    assert records.recover_stale() == 1
    assert records.get(stale_id)["error_code"] == "worker_interrupted"
    with pytest.raises(InvalidTransition, match="claim_lost"):
        records.finish(stale_id, {}, None)
