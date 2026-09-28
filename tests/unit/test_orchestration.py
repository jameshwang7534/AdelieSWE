"""Dependency scheduling rules and Celery registration without external services."""

from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.orchestration.state import InvalidTransition, advance
from app.services.task_queue import CeleryTaskQueue
from app.workers.factory import create_celery_app
from app.workers.orchestration import RECONCILE_TASK_NAME, RECOVER_TASK_NAME


def test_independent_roots_and_fan_in() -> None:
    graph = {"A": (), "B": (), "C": ("A", "B"), "D": ("C",)}
    state = advance(graph, dict.fromkeys(graph, "pending"))
    assert state == {"A": "queued", "B": "queued", "C": "pending", "D": "pending"}
    state["A"] = "completed"
    assert advance(graph, state)["C"] == "pending"
    state["B"] = "completed"
    assert advance(graph, state)["C"] == "queued"
    state["B"] = "failed"
    assert advance(graph, state) == {
        "A": "completed",
        "B": "failed",
        "C": "blocked",
        "D": "blocked",
    }


def test_repeated_reconciliation_and_invalid_graph() -> None:
    graph = {"A": (), "B": ("A",)}
    state = advance(graph, {"A": "running", "B": "pending"})
    assert state == advance(graph, state)
    with pytest.raises(InvalidTransition):
        advance(graph, {"A": "pending"})
    with pytest.raises(ValueError):
        advance({"A": ("B",), "B": ("A",)}, dict.fromkeys(graph, "pending"))


def test_orchestration_celery_configuration() -> None:
    app = create_celery_app(
        Settings(
            celery_broker_url=SecretStr("redis://localhost:6379/1"),
            celery_result_backend=SecretStr("redis://localhost:6379/2"),
        )
    )
    try:
        for name in (RECONCILE_TASK_NAME, RECOVER_TASK_NAME):
            assert app.conf.task_routes[name] == {"queue": "orchestration"}
            assert app.tasks[name].acks_late and app.tasks[name].reject_on_worker_lost
        assert app.conf.beat_schedule["recover-executions"]["task"] == RECOVER_TASK_NAME
        assert app.conf.beat_schedule["recover-executions"]["schedule"] == 30
    finally:
        app.close()
    application = Mock()
    identifier = uuid4()
    CeleryTaskQueue(application).enqueue_execution(identifier)
    application.send_task.assert_called_once_with(
        RECONCILE_TASK_NAME, args=[str(identifier)], queue="orchestration"
    )
