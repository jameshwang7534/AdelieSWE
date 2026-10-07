"""Only committed transitions emit events; elapsed run status survives reload."""

import logging
import os
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from app.core.observability import bind
from app.services.executions import ExecutionService
from tests.integration.test_database import engine as engine
from tests.integration.test_database import factory as factory
from tests.integration.test_executions import make_plan

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Requires PostgreSQL"
)


def test_committed_transitions_and_execution_duration(
    factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="app.events")
    execution = ExecutionService(factory)
    identifier = execution.create(make_plan(factory))
    correlation = str(uuid4())
    with bind(correlation_id=correlation):
        state = execution.reconcile(identifier)

        def fail_commit(session: Session) -> None:
            raise RuntimeError("synthetic commit failure")

        caplog.clear()
        event.listen(Session, "before_commit", fail_commit)
        try:
            with pytest.raises(RuntimeError):
                execution.transition(identifier, state.tasks[0].id, "running")
        finally:
            event.remove(Session, "before_commit", fail_commit)
        assert not caplog.records
        for _ in range(len(state.tasks)):
            ready = next(t for t in execution.reconcile(identifier).tasks if t.status == "queued")
            execution.transition(identifier, ready.id, "running")
            execution.transition(identifier, ready.id, "completed")
    logs = [r for r in caplog.records if r.getMessage() == "state.transition"]
    assert logs
    terminal = [r for r in logs if getattr(r, "duration_ms", None) is not None]
    assert len(terminal) == 1
    assert terminal[0].__dict__["execution_id"] == identifier
    assert terminal[0].__dict__["duration_ms"] >= 0
    assert terminal[0].__dict__["to_state"] == "completed"
    response = ExecutionService(factory).get(identifier)
    assert response.elapsed_seconds is not None and response.elapsed_seconds >= 0
    assert response.task_counts == {"completed": len(state.tasks)}
