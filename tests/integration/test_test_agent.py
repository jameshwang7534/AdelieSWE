"""Fake and real Docker test execution after coding, with PostgreSQL state assertions."""

import os
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.coding import CodingAgent
from app.core.config import Settings
from app.db.session import session_scope
from app.integrations.llm.fake_provider import FakeLLMProvider
from app.models import AgentRun, TaskExecution
from app.orchestration.state import InvalidTransition
from app.sandbox.base import SandboxRequest, SandboxResult
from app.sandbox.docker import DockerSandbox
from app.schemas.testing import RepositoryTestConfig
from app.services.code_patches import CodePatchService
from app.services.coding import CodingService
from app.services.coding_records import CodingRecords
from app.services.executions import ExecutionService
from app.services.test_records import TestRecords as Records
from app.services.testing import TestExecutionError as ExecutionError
from app.services.testing import TestService as Service
from tests.integration.test_coding import setup_run
from tests.integration.test_database import engine as engine
from tests.integration.test_database import factory as factory
from tests.integration.test_sandbox import InspectingCLI
from tests.unit.test_code_patches import change

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Requires PostgreSQL"
)


class FakeSandbox:
    def __init__(self, results: list[SandboxResult | Exception]) -> None:
        self.results = results
        self.calls: list[SandboxRequest] = []

    def execute(self, request: SandboxRequest) -> SandboxResult:
        self.calls.append(request)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["pass", "fail", "timeout", "error", "policy"])
async def test_persisted_test_outcomes(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path, outcome: str
) -> None:
    run_id, task_id, context, workspace = setup_run(factory, tmp_path, local_repository)
    settings = Settings(workspace_root=tmp_path)
    await CodingService(
        settings,
        CodingAgent(FakeLLMProvider(change().model_dump_json())),
        CodingRecords(factory),
        CodePatchService(tmp_path),
    ).run(run_id, task_id, context)
    result = SandboxResult(
        exit_code=0 if outcome in {"pass", "timeout"} else 1,
        stdout="test output",
        stderr="test diagnostic",
        elapsed_seconds=0.25,
        timed_out=outcome == "timeout",
        output_truncated=False,
    )
    scheduler = ExecutionService(factory)
    other = scheduler.get(run_id).tasks[1]
    scheduler.transition(run_id, other.id, "running")
    scheduler.transition(run_id, other.id, "completed", "fixture verified")
    fake = FakeSandbox([RuntimeError("private diagnostic") if outcome == "error" else result])
    config = RepositoryTestConfig(
        required_commands=("pytest; echo injected" if outcome == "policy" else "pytest",)
    )
    service = Service(settings, fake, Records(factory))
    if outcome in {"error", "policy"}:
        with pytest.raises(ExecutionError):
            service.run(run_id, task_id, config)
    else:
        report = service.run(run_id, task_id, config)
        assert report.passed == (outcome == "pass")
        assert report.results[0].duration == 0.25
        assert report.results[0].stdout == "test output"
        assert report.results[0].stderr == "test diagnostic"
    with session_scope(factory) as session:
        task = session.get(TaskExecution, task_id)
        agent = session.scalar(select(AgentRun).where(AgentRun.agent_type == "test"))
        assert task is not None and task.status == ("completed" if outcome == "pass" else "failed")
        assert agent is not None and agent.completed_at is not None
        assert agent.status == ("completed" if outcome == "pass" else "failed")
        assert "private diagnostic" not in str(agent.output_metadata)
    assert len(fake.calls) == (0 if outcome == "policy" else 1)
    assert scheduler.get(run_id).tasks[2].status == ("queued" if outcome == "pass" else "blocked")
    if fake.calls:
        assert fake.calls[0].workspace == workspace and fake.calls[0].environment == {}
    with pytest.raises(InvalidTransition):
        service.run(run_id, task_id, config)


@pytest.mark.asyncio
async def test_all_required_checks_must_pass(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path
) -> None:
    run_id, task_id, context, _ = setup_run(factory, tmp_path, local_repository)
    settings = Settings(workspace_root=tmp_path)
    await CodingService(
        settings,
        CodingAgent(FakeLLMProvider(change().model_dump_json())),
        CodingRecords(factory),
        CodePatchService(tmp_path),
    ).run(run_id, task_id, context)
    fake = FakeSandbox(
        [
            SandboxResult(
                exit_code=code,
                stdout="",
                stderr="",
                elapsed_seconds=0.1,
                timed_out=False,
                output_truncated=False,
            )
            for code in (0, 1)
        ]
    )
    report = Service(settings, fake, Records(factory)).run(
        run_id,
        task_id,
        RepositoryTestConfig(required_commands=("pytest", "python -m unittest")),
    )
    assert len(fake.calls) == 2 and not report.passed
    assert [r.passed for r in report.results] == [True, False]
    with session_scope(factory) as session:
        task = session.get(TaskExecution, task_id)
        assert task is not None and task.status == "failed"


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("RUN_SANDBOX_TESTS") != "1", reason="Requires Linux Docker")
@pytest.mark.parametrize("outcome", ["pass", "fail", "timeout", "missing", "flood"])
async def test_real_docker_test_agent(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path, outcome: str
) -> None:
    run_id, task_id, context, workspace = setup_run(factory, tmp_path, local_repository)
    settings = Settings(
        workspace_root=tmp_path,
        sandbox_timeout_seconds=1 if outcome == "timeout" else 30,
        sandbox_output_bytes=1024,
    )
    await CodingService(
        settings,
        CodingAgent(FakeLLMProvider(change().model_dump_json())),
        CodingRecords(factory),
        CodePatchService(tmp_path),
    ).run(run_id, task_id, context)
    body = {
        "pass": "self.assertEqual(sys.platform, 'linux'); self.assertEqual(os.getuid(), 1000)",
        "fail": "self.fail('fixture failure')",
        "timeout": "time.sleep(20)",
        "missing": "self.fail('Must not run when executable is missing')",
        "flood": "print('x' * 10000); print('y' * 10000, file=sys.stderr)",
    }[outcome]
    (workspace / "test_fixture.py").write_text(
        "import unittest, time, sys, os\nclass Fixture(unittest.TestCase):\n"
        f"    def test_fixture(self):\n        {body}\n",
        encoding="utf-8",
        newline="\n",
    )
    runner = InspectingCLI()
    command = "pytest" if outcome == "missing" else "python -m unittest"
    report = Service(settings, DockerSandbox(settings, runner), Records(factory)).run(
        run_id, task_id, RepositoryTestConfig(required_commands=(command,))
    )
    assert report.passed == (outcome in {"pass", "flood"})
    assert report.results[0].timeout == (outcome == "timeout")
    if outcome == "pass":
        assert "Ran 1 test" in report.results[0].stderr and "OK" in report.results[0].stderr
    if outcome == "fail":
        assert "fixture failure" in report.results[0].stderr
    if outcome == "missing":
        assert report.results[0].exit_code == 127
        assert "pytest" in report.results[0].stderr
    if outcome == "flood":
        assert report.results[0].output_truncated
        assert len(report.results[0].stdout.encode()) == 1024
        assert len(report.results[0].stderr.encode()) == 1024
    assert report.results[0].duration > 0
    assert len(runner.inspected) == 1
    metadata = runner.inspected[0]
    config = metadata["Config"]
    host = metadata["HostConfig"]
    assert isinstance(config, dict) and isinstance(host, dict)
    assert config["Entrypoint"] == ["/usr/bin/env"]
    assert config["Cmd"][-len(report.results[0].command) :] == list(report.results[0].command)
    assert config["User"] == "1000:1000" and host["NetworkMode"] == "none"
    assert not host["Privileged"]
    assert runner.run(["inspect", runner.names[0]], 5, 1024).code != 0
    with session_scope(factory) as session:
        task = session.get(TaskExecution, task_id)
        assert task is not None and task.status == ("completed" if report.passed else "failed")
        agent = session.scalar(select(AgentRun).where(AgentRun.agent_type == "test"))
        assert agent is not None and agent.completed_at is not None
        assert agent.output_metadata["report"] == report.model_dump(mode="json")
        assert agent.status == ("completed" if report.passed else "failed")
