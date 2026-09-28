"""Exact command profiles cannot admit injected flags, paths, or shell programs."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agents.testing import COMMANDS
from app.agents.testing import TestAgent as Agent
from app.agents.testing import TestPolicyError as PolicyError
from app.schemas.context import ContextRepository
from app.schemas.planning import PlanTaskProposal
from app.schemas.testing import RepositoryTestConfig
from app.schemas.testing import TestInput as AgentInput


def inputs(commands: tuple[str, ...]) -> AgentInput:
    return AgentInput(
        repository=ContextRepository(
            id=uuid4(),
            github_owner="fixture",
            github_name="repo",
            default_branch="main",
            index_status="complete",
            embedding_status="pending",
        ),
        task=PlanTaskProposal(
            task_key="A",
            title="Fix",
            description="Fix",
            rationale="Bug",
            target_files=("a.py",),
            dependencies=(),
            acceptance_criteria=("Works",),
            suggested_tests=(),
        ),
        coding_diff="+change",
        suggested_tests=("sh -c evil",),
        configuration=RepositoryTestConfig(required_commands=commands),
    )


@pytest.mark.parametrize("name", list(COMMANDS))
def test_known_commands(name: str) -> None:
    selected = Agent(tuple(COMMANDS)).select(inputs((name, name)))
    assert len(selected) == 1
    assert (selected[0].image, selected[0].command) == COMMANDS[name]


@pytest.mark.parametrize(
    "name",
    [
        "pytest; echo evil",
        "pytest && whoami",
        "python -c evil",
        "sh",
        "npm install",
        "npm test -- --eval evil",
        "pytest -p malicious",
        "../pytest",
        "/bin/pytest",
        "pytest\nwhoami",
        "$(whoami)",
        "cargo test --target-dir /host",
    ],
)
def test_reject_untrusted_command(name: str) -> None:
    with pytest.raises(PolicyError, match="test_command_not_allowed"):
        Agent(tuple(COMMANDS)).select(inputs((name,)))


def test_policy_and_required_checks_cannot_be_weakened() -> None:
    with pytest.raises(ValidationError):
        RepositoryTestConfig(required_commands=())
    with pytest.raises(PolicyError):
        Agent(("sh",))
    with pytest.raises(PolicyError):
        Agent(("pytest",)).select(inputs(("npm test",)))
