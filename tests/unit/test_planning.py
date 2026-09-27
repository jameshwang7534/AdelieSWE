"""DAG validation covers chains, diamonds, cycles, invalid references, and stable order."""

from itertools import permutations
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.planning import ImplementationPlanProposal, PlanTaskProposal
from app.services.plans import PlanService


def task(key: str, dependencies: tuple[str, ...] = ()) -> PlanTaskProposal:
    return PlanTaskProposal(
        task_key=key,
        title=f"Implement {key}",
        description="Make the change",
        rationale="Required by the issue",
        target_files=("app/example.py",),
        dependencies=dependencies,
        acceptance_criteria=("Behavior verified",),
        suggested_tests=("Run the relevant unit tests",),
    )


def proposal(*tasks: PlanTaskProposal) -> ImplementationPlanProposal:
    return ImplementationPlanProposal(summary="Fixture implementation", tasks=tasks)


@pytest.mark.parametrize(
    "edges,expected",
    [
        ({"TASK-1": ()}, ["TASK-1"]),
        (
            {"TASK-3": ("TASK-2",), "TASK-2": ("TASK-1",), "TASK-1": ()},
            ["TASK-1", "TASK-2", "TASK-3"],
        ),
        ({"D": ("B", "C"), "C": ("A",), "B": ("A",), "A": ()}, ["A", "B", "C", "D"]),
        ({"C": (), "B": (), "A": ()}, ["A", "B", "C"]),
        ({"C": ("A",), "B": ("A",), "A": ()}, ["A", "B", "C"]),
        ({"C": ("A", "B"), "B": (), "A": ()}, ["A", "B", "C"]),
        ({"D": ("C",), "C": (), "B": ("A",), "A": ()}, ["A", "C", "B", "D"]),
    ],
)
def test_valid_dags(edges: dict[str, tuple[str, ...]], expected: list[str]) -> None:
    result = proposal(*(task(key, deps) for key, deps in edges.items()))
    assert result.execution_order() == expected
    positions = {key: index for index, key in enumerate(expected)}
    for key, parents in edges.items():
        assert all(positions[parent] < positions[key] for parent in parents)


def test_example_order_independent_of_input() -> None:
    tasks = [
        task("TASK-1"),
        task("TASK-2", ("TASK-1",)),
        task("TASK-3", ("TASK-2",)),
        task("TASK-4", ("TASK-2", "TASK-3")),
    ]
    for ordering in permutations(tasks):
        assert proposal(*ordering).execution_order() == ["TASK-1", "TASK-2", "TASK-3", "TASK-4"]


@pytest.mark.parametrize(
    "tasks,reason",
    [
        ([], "at least 1"),
        ([task("A"), task("A")], "unique"),
        ([task("A", ("missing",))], "unknown"),
        ([task("A", ("A",))], "itself"),
        ([task("A", ("B",)), task("B", ("A",))], "cycle"),
        ([task("A", ("C",)), task("B", ("A",)), task("C", ("B",))], "cycle"),
        ([task("ROOT"), task("A", ("B",)), task("B", ("A",))], "cycle"),
        ([task("A"), task("B", ("A", "A"))], "Duplicate"),
        ([task("A"), task("B", ("a",))], "unknown"),
    ],
)
def test_invalid_dags(tasks: list[PlanTaskProposal], reason: str) -> None:
    with pytest.raises(ValidationError, match=reason):
        proposal(*tasks)


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_key", " "),
        ("task_key", "invalid key"),
        ("title", " "),
        ("description", ""),
        ("rationale", ""),
        ("acceptance_criteria", []),
        ("suggested_tests", [""]),
        ("unexpected", True),
    ],
)
def test_invalid_task_fields(field: str, value: object) -> None:
    data = task("A").model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        PlanTaskProposal.model_validate(data)


def test_bounds_immutability_roundtrip_and_revalidation() -> None:
    valid = proposal(task("A"))
    assert ImplementationPlanProposal.model_validate_json(valid.model_dump_json()) == valid
    with pytest.raises(ValidationError):
        valid.summary = "changed"
    with pytest.raises(ValidationError):
        ImplementationPlanProposal(summary=" ", tasks=valid.tasks)
    with pytest.raises(ValidationError):
        proposal(*(task(f"TASK-{i}") for i in range(201)))
    invalid = valid.model_copy(update={"tasks": (task("A", ("missing",)),)})
    sessions = Mock()
    with pytest.raises(ValidationError):
        PlanService(sessions).create(uuid4(), invalid)
    sessions.begin.assert_not_called()


def test_long_chain() -> None:
    tasks = [task(f"T{i}", (f"T{i - 1}",) if i else ()) for i in range(200)]
    assert proposal(*reversed(tasks)).execution_order() == [f"T{i}" for i in range(200)]
