"""PostgreSQL proposal persistence, preserved metadata, and atomic rollback."""

import os
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope
from app.models import ImplementationPlan, PlanTask
from app.schemas.planning import ImplementationPlanProposal, PlanTaskProposal
from app.services.plans import PlanService
from app.services.repositories import RecordNotFound
from tests.integration.test_database import engine as engine
from tests.integration.test_database import factory as factory
from tests.integration.test_database import graph as graph
from tests.unit.test_planning import proposal, task

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Requires PostgreSQL"
)


@pytest.mark.parametrize(
    "tasks,expected",
    [
        ((task("A"),), ["A"]),
        ((task("C", ("B",)), task("B", ("A",)), task("A")), ["A", "B", "C"]),
        ((task("C", ("A",)), task("B", ("A",)), task("A")), ["A", "B", "C"]),
        ((task("C", ("A", "B")), task("B"), task("A")), ["A", "B", "C"]),
        ((task("A", ("A",)),), None),
        ((task("A", ("missing",)),), None),
        ((task("A"), task("A")), None),
        ((task("A", ("B",)), task("B", ("A",))), None),
        ((task("A", ("C",)), task("B", ("A",)), task("C", ("B",))), None),
    ],
    ids=[
        "single",
        "linear",
        "fan-out",
        "fan-in",
        "self",
        "missing",
        "duplicate",
        "direct-cycle",
        "long-cycle",
    ],
)
def test_all_audit_cases_at_persistence_boundary(
    factory: sessionmaker[Session],
    graph: dict[str, UUID],
    tasks: tuple[PlanTaskProposal, ...],
    expected: list[str] | None,
) -> None:
    service = PlanService(factory)
    if expected is None:
        # Deliberately bypass schema construction to challenge the persistence boundary.
        invalid = ImplementationPlanProposal.model_construct(summary="Invalid audit", tasks=tasks)
        with session_scope(factory) as session:
            before_plans = set(session.scalars(select(ImplementationPlan.id)))
            before_tasks = set(session.scalars(select(PlanTask.id)))
        with pytest.raises(ValidationError):
            service.create(graph["issue"], invalid)
        with session_scope(factory) as session:
            assert set(session.scalars(select(ImplementationPlan.id))) == before_plans
            assert set(session.scalars(select(PlanTask.id))) == before_tasks
    else:
        submitted = proposal(*tasks)
        assert submitted.execution_order() == expected
        identifier = service.create(graph["issue"], submitted)
        with session_scope(factory) as session:
            saved = list(
                session.scalars(
                    select(PlanTask)
                    .where(PlanTask.plan_id == identifier)
                    .order_by(PlanTask.sequence)
                )
            )
            assert [item.task_key for item in saved] == expected
            assert [item.sequence for item in saved] == list(range(len(expected)))
            positions = {item.task_key: item.sequence for item in saved}
            assert all(
                positions[parent] < item.sequence for item in saved for parent in item.dependencies
            )


def test_persist_validated_plan(factory: sessionmaker[Session], graph: dict[str, UUID]) -> None:
    submitted = proposal(
        task("TASK-4", ("TASK-2", "TASK-3")),
        task("TASK-3", ("TASK-2",)),
        task("TASK-2", ("TASK-1",)),
        task("TASK-1"),
    )
    identifier = PlanService(factory).create(graph["issue"], submitted)
    with session_scope(factory) as session:
        plan = session.get(ImplementationPlan, identifier)
        assert plan is not None and plan.issue_id == graph["issue"] and plan.status == "draft"
        assert plan.summary == submitted.summary and isinstance(plan.id, UUID)
        tasks = list(
            session.scalars(
                select(PlanTask).where(PlanTask.plan_id == identifier).order_by(PlanTask.sequence)
            )
        )
        assert [item.task_key for item in tasks] == submitted.execution_order()
        assert [item.sequence for item in tasks] == [0, 1, 2, 3]
        originals = {item.task_key: item for item in submitted.tasks}
        for item in tasks:
            original = originals[item.task_key]
            assert item.status == "pending" and item.created_at.tzinfo is not None
            for name in ("title", "description", "rationale"):
                assert getattr(item, name) == getattr(original, name)
            for name in ("dependencies", "target_files", "acceptance_criteria", "suggested_tests"):
                assert getattr(item, name) == list(getattr(original, name))
        legacy = session.get(PlanTask, graph["task"])
        assert legacy is not None and legacy.rationale is None
        assert legacy.acceptance_criteria == legacy.suggested_tests == []


def test_missing_issue_and_transaction_rollback(
    factory: sessionmaker[Session], graph: dict[str, UUID]
) -> None:
    service = PlanService(factory)
    with pytest.raises(RecordNotFound):
        service.create(uuid4(), proposal(task("A")))
    with session_scope(factory) as session:
        before = set(session.scalars(select(ImplementationPlan.id)))
    original = Session.flush

    def fail_tasks(session: Session, objects: object = None) -> None:
        if any(isinstance(item, PlanTask) for item in session.new):
            raise RuntimeError("simulated-task-write-failure")
        original(session)

    with patch.object(Session, "flush", fail_tasks):
        with pytest.raises(RuntimeError, match="simulated-task-write-failure"):
            service.create(graph["issue"], proposal(task("A")))
    with session_scope(factory) as session:
        assert set(session.scalars(select(ImplementationPlan.id))) == before
