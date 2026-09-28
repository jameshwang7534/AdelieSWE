"""Real PostgreSQL orchestration, restart, concurrency, API, and fake executor tests."""

import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from celery.contrib.testing.worker import start_worker
from fastapi.testclient import TestClient
from kombu import Queue
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.session import session_scope
from app.main import create_app
from app.models import ExecutionRun, Issue, PlanTask, Repository, TaskExecution
from app.orchestration.executor import ExecutorFailure, Orchestrator
from app.orchestration.state import InvalidTransition
from app.schemas.executions import ExecutionResponse, TaskExecutionResponse
from app.services.executions import ExecutionService, InvalidExecutionPlan
from app.services.plans import PlanService
from app.services.task_queue import TaskQueue
from app.workers.factory import create_celery_app
from app.workers.orchestration import RECONCILE_TASK_NAME, RECOVER_TASK_NAME
from tests.integration.test_database import engine as engine
from tests.integration.test_database import factory as factory
from tests.unit.test_planning import proposal, task

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Requires PostgreSQL"
)


@pytest.mark.skipif(os.environ.get("RUN_WORKER_TESTS") != "1", reason="Requires Redis worker")
def test_duplicate_celery_delivery_and_recovery(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = ExecutionService(sessions)
    identifier = service.create(make_plan(sessions, diamond=True))
    monkeypatch.setenv("DATABASE_URL", engine.url.render_as_string(hide_password=False))
    application = create_celery_app(Settings())
    queue_name = f"orchestration-test-{uuid4().hex}"
    application.conf.task_queues = (Queue(queue_name),)
    application.conf.task_default_queue = queue_name
    application.conf.task_routes = {
        name: {"queue": queue_name} for name in (RECONCILE_TASK_NAME, RECOVER_TASK_NAME)
    }
    message_id = str(uuid4())
    recovery_id = str(uuid4())

    def wait_result(task_id: str) -> object:
        # Poll persisted results: Redis pubsub subscriptions can race when reusing a task ID.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            metadata = application.backend.get_task_meta(task_id, cache=False)
            if metadata["status"] == "SUCCESS":
                return metadata["result"]
            assert metadata["status"] != "FAILURE", "Orchestration task failed"
            time.sleep(0.05)
        pytest.fail("Orchestration task did not finish within 20 seconds")

    try:
        with start_worker(application, pool="solo", perform_ping_check=False, queues=[queue_name]):
            # Same Celery ID and payload delivered twice; inspect each acknowledged execution.
            for _ in range(2):
                result = application.send_task(
                    RECONCILE_TASK_NAME,
                    args=[str(identifier)],
                    queue=queue_name,
                    task_id=message_id,
                )
                value = ExecutionResponse.model_validate(wait_result(message_id))
                assert [item.status for item in value.tasks] == [
                    "queued",
                    "pending",
                    "pending",
                    "pending",
                ]
                result.forget()
            state = service.get(identifier)
            assert len(state.tasks) == 4
            assert service.transition(identifier, state.tasks[0].id, "running")
            service.transition(identifier, state.tasks[0].id, "completed")
            result = application.send_task(RECOVER_TASK_NAME, queue=queue_name, task_id=recovery_id)
            recovered = wait_result(recovery_id)
            assert isinstance(recovered, dict) and recovered["reconciled"] >= 1
            assert [item.status for item in ExecutionService(sessions).get(identifier).tasks] == [
                "completed",
                "queued",
                "queued",
                "pending",
            ]
            with session_scope(sessions) as session:
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(TaskExecution)
                        .where(TaskExecution.execution_run_id == identifier)
                    )
                    == 4
                )
            print(
                "Celery: duplicate message ID delivered twice; 4 task rows; "
                "recovery preserved B/C queued"
            )
    finally:
        try:
            application.backend.forget(message_id)
            application.backend.forget(recovery_id)
            with application.connection_for_write() as connection:
                Queue(queue_name)(connection.default_channel).delete(if_unused=True, if_empty=True)
        finally:
            application.backend.result_consumer.stop()
            application.backend.client.close()
            application.close()


def make_plan(sessions: sessionmaker[Session], diamond: bool = False) -> UUID:
    with session_scope(sessions) as session:
        repo = Repository(
            github_owner="orchestration",
            github_name=str(uuid4()),
            clone_url="https://example.invalid/test",
            default_branch="main",
        )
        session.add(repo)
        session.flush()
        issue = Issue(
            repository_id=repo.id,
            github_issue_number=1,
            title="Test orchestration",
            source_url="https://example.invalid/issues/1",
        )
        session.add(issue)
        session.flush()
        identifier = issue.id
    tasks = (
        (task("D", ("B", "C")), task("C", ("A",)), task("B", ("A",)), task("A"))
        if diamond
        else (task("D", ("C",)), task("C", ("A", "B")), task("B"), task("A"))
    )
    return PlanService(sessions).create(identifier, proposal(*tasks))


@pytest.mark.parametrize("failure", [None, "A", "B"], ids=["success", "A-fails", "B-fails"])
def test_diamond_audit(factory: sessionmaker[Session], failure: str | None) -> None:
    service = ExecutionService(factory)
    identifier = service.create(make_plan(factory, diamond=True))
    initial = service.reconcile(identifier)
    ids = {item.task_key: item.id for item in initial.tasks}

    def record(label: str, expected: list[str]) -> None:
        state = ExecutionService(factory).get(identifier)
        assert [item.status for item in state.tasks] == expected
        print(
            f"{failure or 'success'} / {label}: "
            + ", ".join(f"{item.task_key}={item.status}" for item in state.tasks)
        )
        assert service.reconcile(identifier) == state

    record("initial", ["queued", "pending", "pending", "pending"])
    with pytest.raises(InvalidTransition):
        service.transition(identifier, ids["D"], "running")
    with pytest.raises(InvalidTransition):
        service.transition(identifier, ids["A"], "completed")
    assert service.transition(identifier, ids["A"], "running")
    assert not ExecutionService(factory).transition(identifier, ids["A"], "running")
    if failure == "A":
        service.transition(identifier, ids["A"], "failed")
        record("A failed", ["failed", "blocked", "blocked", "blocked"])
        assert service.get(identifier).status == "failed"
        return
    service.transition(identifier, ids["A"], "completed")
    record("A completed", ["completed", "queued", "queued", "pending"])
    assert not service.transition(identifier, ids["A"], "running")
    assert not service.transition(identifier, ids["A"], "completed")
    # Independent tasks can both be running before either completes.
    service.transition(identifier, ids["B"], "running")
    service.transition(identifier, ids["C"], "running")
    record("B/C running", ["completed", "running", "running", "pending"])
    service = ExecutionService(factory)  # No in-memory scheduler state survives this restart.
    service.transition(identifier, ids["B"], "failed" if failure == "B" else "completed")
    record(
        "B finished",
        [
            "completed",
            "failed" if failure else "completed",
            "running",
            "blocked" if failure else "pending",
        ],
    )
    service.transition(identifier, ids["C"], "completed")
    if failure:
        record("C completed", ["completed", "failed", "completed", "blocked"])
        assert service.get(identifier).status == "failed"
    else:
        record("C completed", ["completed", "completed", "completed", "queued"])
        service.transition(identifier, ids["D"], "running")
        service.transition(identifier, ids["D"], "completed")
        record("finished", ["completed"] * 4)
        assert service.get(identifier).status == "completed"
    with session_scope(factory) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(TaskExecution)
                .where(TaskExecution.execution_run_id == identifier)
            )
            == 4
        )


class FakeTaskExecutor:
    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail = fail

    def execute(self, task: TaskExecutionResponse) -> None:
        self.calls.append(task.task_key)
        if task.task_key == self.fail:
            raise ExecutorFailure("fixture failure")


def test_waves_restart_snapshot_and_idempotency(factory: sessionmaker[Session]) -> None:
    service = ExecutionService(factory)
    plan_id = make_plan(factory)
    identifier = service.create(plan_id)
    assert service.get(identifier).status == "pending"
    first = service.reconcile(identifier)
    assert [t.status for t in first.tasks] == ["queued", "queued", "pending", "pending"]
    assert service.reconcile(identifier) == first
    # The live plan may change; existing runs keep their validated snapshot.
    with session_scope(factory) as session:
        original = session.scalar(
            select(PlanTask).where(PlanTask.plan_id == plan_id, PlanTask.task_key == "A")
        )
        assert original is not None
        original.dependencies = ["D"]
    with pytest.raises(InvalidExecutionPlan):
        service.create(plan_id)
    restarted = ExecutionService(factory)
    executor = FakeTaskExecutor()
    orchestrator = Orchestrator(restarted, executor)
    assert orchestrator.dispatch_ready(identifier) == 2
    assert [t.status for t in restarted.get(identifier).tasks] == [
        "completed",
        "completed",
        "queued",
        "pending",
    ]
    assert orchestrator.dispatch_ready(identifier) == 1
    assert orchestrator.dispatch_ready(identifier) == 1
    assert orchestrator.dispatch_ready(identifier) == 0
    final = restarted.get(identifier)
    assert final.status == "completed" and final.completed_at is not None
    assert final.started_at is not None and final.completed_at >= final.started_at
    assert executor.calls == ["A", "B", "C", "D"]
    assert not restarted.transition(identifier, final.tasks[0].id, "running")
    with session_scope(factory) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(TaskExecution)
                .where(TaskExecution.execution_run_id == identifier)
            )
            == 4
        )


def test_failure_blocks_descendants_not_independent_tasks(factory: sessionmaker[Session]) -> None:
    service = ExecutionService(factory)
    identifier = service.create(make_plan(factory))
    executor = FakeTaskExecutor("A")
    assert Orchestrator(service, executor).dispatch_ready(identifier) == 2
    state = service.get(identifier)
    assert state.status == "failed"
    assert [t.status for t in state.tasks] == ["failed", "completed", "blocked", "blocked"]
    assert executor.calls == ["A", "B"]
    assert service.reconcile(identifier) == state
    assert not service.transition(identifier, state.tasks[0].id, "failed")
    with pytest.raises(InvalidTransition):
        service.transition(identifier, state.tasks[0].id, "completed")


def test_invalid_transition_and_independent_runs(factory: sessionmaker[Session]) -> None:
    service = ExecutionService(factory)
    plan_id = make_plan(factory)
    first, second = service.create(plan_id), service.create(plan_id)
    a = service.get(first).tasks[0]
    with pytest.raises(InvalidTransition):
        service.transition(first, a.id, "running")
    service.reconcile(first)
    assert service.transition(first, a.id, "running")
    assert not ExecutionService(factory).transition(first, a.id, "running")
    assert service.get(second).status == "pending"
    assert service.get(first).tasks[0].status == "running"
    with pytest.raises(InvalidTransition):
        service.transition(first, service.get(first).tasks[2].id, "completed")


def test_concurrent_schedulers_and_claims(engine: Engine) -> None:
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = ExecutionService(sessions)
    identifier = service.create(make_plan(sessions))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: ExecutionService(sessions).reconcile(identifier), range(8)))
        root = service.get(identifier).tasks[0].id
        claims = list(
            pool.map(
                lambda _: ExecutionService(sessions).transition(identifier, root, "running"),
                range(8),
            )
        )
    assert claims.count(True) == 1
    state = service.get(identifier)
    assert len(state.tasks) == 4 and state.tasks[1].status == "queued"


def test_api_and_broker_outage_recovery(
    factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    service = ExecutionService(factory)
    plan_id = make_plan(factory)
    queue = Mock(spec=TaskQueue)
    queue.enqueue_execution.side_effect = ConnectionError("sensitive fixture")

    @contextmanager
    def resources(settings: Settings) -> Iterator[ExecutionService | None]:
        yield service

    @contextmanager
    def queues(settings: Settings) -> Iterator[TaskQueue | None]:
        yield queue

    settings = Settings(database_url=None, redis_url=None, celery_broker_url=None)
    with TestClient(create_app(settings, execution_factory=resources, queue_factory=queues)) as api:
        response = api.post(f"/plans/{plan_id}/executions")
        assert response.status_code == 202, response.text
        identifier = UUID(response.json()["id"])
        queue.enqueue_execution.assert_called_once_with(identifier)
        assert response.json()["status"] == "pending"
        assert "sensitive fixture" not in caplog.text + response.text
        recovery = ExecutionService(factory)
        assert identifier in recovery.active_ids()
        for run_id in recovery.active_ids():
            recovery.reconcile(run_id)
        assert api.get(f"/executions/{identifier}").json()["tasks"][0]["status"] == "queued"
        assert api.get(f"/executions/{uuid4()}").status_code == 404
        assert api.post(f"/plans/{uuid4()}/executions").status_code == 404
        with session_scope(factory) as session:
            plan_task = session.scalar(select(PlanTask).where(PlanTask.plan_id == plan_id))
            assert plan_task is not None
            plan_task.dependencies = ["missing"]
        assert api.post(f"/plans/{plan_id}/executions").status_code == 409
        with session_scope(factory) as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ExecutionRun)
                    .where(ExecutionRun.plan_id == plan_id)
                )
                == 1
            )
