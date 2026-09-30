"""Deterministic full recovery workflow with fake providers, real Git and PostgreSQL."""

import asyncio
import json
import os
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.coding import CodingAgent
from app.agents.debugging import DebugAgent
from app.core.config import Settings
from app.db.session import session_scope
from app.integrations.llm.fake_provider import FakeLLMProvider
from app.integrations.llm.provider import LLMError, LLMMessage, LLMResult
from app.models import AgentRun, TaskExecution
from app.orchestration.state import InvalidTransition
from app.sandbox.base import SandboxRequest, SandboxResult
from app.schemas.testing import RepositoryTestConfig
from app.services.code_patches import CodePatchService
from app.services.coding import CodingService
from app.services.coding_records import CodingRecords
from app.services.executions import ExecutionService
from app.services.recovery import RecoveryError, RecoveryService
from app.services.recovery_records import RecoveryRecords
from app.services.test_records import TestRecords as Records
from app.services.testing import TestService as ValidationService
from tests.integration.test_coding import setup_run
from tests.integration.test_database import engine as engine
from tests.integration.test_database import factory as factory
from tests.unit.test_code_patches import EDIT, change

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Requires PostgreSQL"
)
CONFIG = RepositoryTestConfig(required_commands=("python -m unittest",))


def fix(before: str, after: str) -> str:
    return change(
        EDIT.replace("-original", f"-{before}").replace("+changed", f"+{after}")
    ).model_dump_json()


class CheckingSandbox:
    def __init__(self, factory: sessionmaker[Session], run_id: UUID, expected: str) -> None:
        self.factory, self.run_id, self.expected = factory, run_id, expected
        self.calls: list[SandboxRequest] = []

    def execute(self, request: SandboxRequest) -> SandboxResult:
        state = ExecutionService(self.factory).get(self.run_id)
        assert state.tasks[0].status == "running" and state.tasks[2].status == "pending"
        self.calls.append(request)
        passed = (request.workspace / "hello.txt").read_text() == self.expected + "\n"
        return SandboxResult(
            exit_code=0 if passed else 1,
            stdout="fixture test output",
            stderr="" if passed else "Expected repaired fixture",
            elapsed_seconds=0.01,
            timed_out=False,
            output_truncated=False,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "initial-pass",
        "one-fix",
        "two-fixes",
        "duplicate",
        "malformed",
        "unsafe",
        "zero-budget",
        "corrected",
        "conflict",
        "last-fix",
        "exhausted",
    ],
)
async def test_bounded_recovery(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path, case: str
) -> None:
    run_id, task_id, context, workspace = setup_run(factory, tmp_path, local_repository)
    settings = Settings(
        workspace_root=tmp_path, max_recovery_attempts=0 if case == "zero-budget" else 3
    )
    scheduler = ExecutionService(factory)
    other = scheduler.get(run_id).tasks[1]
    scheduler.transition(run_id, other.id, "running")
    scheduler.transition(run_id, other.id, "completed")
    outputs = {
        "initial-pass": ["unused"],
        "one-fix": [fix("changed", "fixed")],
        "two-fixes": [fix("changed", "middle"), fix("middle", "fixed")],
        "duplicate": [fix("changed", "middle")],
        "malformed": ["not JSON"],
        "unsafe": [fix("changed", "fixed").replace("hello.txt", "../outside")],
        "zero-budget": ["unused"],
        "corrected": ["not JSON", fix("changed", "fixed")],
        "conflict": [fix("wrong-context", "fixed")],
        "last-fix": [fix("changed", "middle"), fix("middle", "later"), fix("later", "fixed")],
        "exhausted": [
            fix("changed", "middle"),
            fix("middle", "later"),
            fix("later", "still-wrong"),
        ],
    }[case]
    provider = FakeLLMProvider(outputs)
    sandbox = CheckingSandbox(factory, run_id, "changed" if case == "initial-pass" else "fixed")
    coding = CodingService(
        settings,
        CodingAgent(FakeLLMProvider(change().model_dump_json())),
        CodingRecords(factory),
        CodePatchService(tmp_path),
    )
    service = RecoveryService(
        settings,
        coding,
        ValidationService(settings, sandbox, Records(factory)),
        DebugAgent(provider),
        RecoveryRecords(factory),
    )
    report = await service.run(run_id, task_id, context, CONFIG)
    passed = case in {"initial-pass", "one-fix", "two-fixes", "corrected", "last-fix"}
    count = {"initial-pass": 0, "one-fix": 1, "two-fixes": 2, "zero-budget": 0, "corrected": 2}.get(
        case, 3
    )
    assert report.passed == passed and report.debug_attempts == count
    assert len(provider.calls) == count
    assert all(call.command == ("python", "-m", "unittest", "discover") for call in sandbox.calls)
    assert scheduler.get(run_id).tasks[2].status == ("queued" if passed else "blocked")
    with session_scope(factory) as session:
        records = list(
            session.scalars(select(AgentRun).where(AgentRun.task_execution_id == task_id))
        )
        debug = sorted(
            [a for a in records if a.agent_type == "debug"],
            key=lambda a: int(str(a.input_metadata["attempt"])),
        )
        tests = [a for a in records if a.agent_type == "test"]
        parent = next(a for a in records if a.agent_type == "recovery")
        task_record = session.get(TaskExecution, task_id)
        assert task_record is not None and task_record.status == (
            "completed" if passed else "failed"
        )
        assert len(debug) == count and all(a.completed_at is not None for a in debug)
        assert parent.output_metadata["debug_attempts"] == count
        assert parent.status == ("completed" if passed else "failed")
        assert len(tests) == len(report.tests)
        if case != "initial-pass":
            assert any(a.status == "failed" for a in tests)
        if case == "duplicate":
            assert len(tests) == 2 and (workspace / "hello.txt").read_text() == "middle\n"
            assert [a.output_metadata.get("error") for a in debug] == [
                None,
                "identical_debug_patch",
                "identical_debug_patch",
            ]
    if count:
        sent = json.loads(provider.calls[0][1].content)
        assert sent["context"]["issue"]["id"] == str(context.issue.id)
        assert "+changed" in sent["current_diff"]
        assert sent["failing_tests"][0]["stderr"] == "Expected repaired fixture"
        if count > 1:
            sent = json.loads(provider.calls[1][1].content)
            assert len(sent["previous_attempts"]) == 1
            if case in {"two-fixes", "last-fix", "exhausted", "duplicate"}:
                assert "+middle" in sent["current_diff"]
    with pytest.raises(InvalidTransition):
        await service.run(run_id, task_id, context, CONFIG)
    assert len(provider.calls) == count


class FailingProvider:
    def __init__(self, hang: bool) -> None:
        self.hang, self.calls = hang, 0

    async def generate[T: BaseModel](
        self, messages: Sequence[LLMMessage], response_model: type[T]
    ) -> LLMResult[T]:
        self.calls += 1
        if self.hang:
            await asyncio.Event().wait()
        raise LLMError("llm_unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize("hang", [False, True])
async def test_llm_failure_consumes_budget(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path, hang: bool
) -> None:
    run_id, task_id, context, _ = setup_run(factory, tmp_path, local_repository)
    settings = Settings(workspace_root=tmp_path, max_recovery_attempts=2, llm_timeout_seconds=1)
    provider = FailingProvider(hang)
    sandbox = CheckingSandbox(factory, run_id, "fixed")
    coding = CodingService(
        settings,
        CodingAgent(FakeLLMProvider(change().model_dump_json())),
        CodingRecords(factory),
        CodePatchService(tmp_path),
    )
    report = await RecoveryService(
        settings,
        coding,
        ValidationService(settings, sandbox, Records(factory)),
        DebugAgent(provider),
        RecoveryRecords(factory),
    ).run(run_id, task_id, context, CONFIG)
    assert not report.passed and report.debug_attempts == provider.calls == 2
    assert len(sandbox.calls) == 1
    with session_scope(factory) as session:
        attempts = list(session.scalars(select(AgentRun).where(AgentRun.agent_type == "debug")))
        assert len(attempts) == 2
        assert all(
            a.output_metadata["error"] == ("llm_timeout" if hang else "llm_unavailable")
            for a in attempts
        )


@pytest.mark.asyncio
async def test_persisted_ownership_budget_and_test_policy(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path
) -> None:
    run_id, task_id, context, _ = setup_run(factory, tmp_path, local_repository)
    settings = Settings(workspace_root=tmp_path)
    records = RecoveryRecords(factory)
    parent, _, _ = records.open(run_id, task_id, context, CONFIG, 1)
    with pytest.raises(InvalidTransition, match="recovery_unavailable"):
        RecoveryRecords(factory).open(run_id, task_id, context, CONFIG, 10)
    coding = CodingService(
        settings,
        CodingAgent(FakeLLMProvider(change().model_dump_json())),
        CodingRecords(factory),
        CodePatchService(tmp_path),
    )
    await coding.run(run_id, task_id, context)
    tests = ValidationService(settings, CheckingSandbox(factory, run_id, "fixed"), Records(factory))
    with pytest.raises(InvalidTransition, match="recovery_owns_tests"):
        tests.run(run_id, task_id, CONFIG)
    with pytest.raises(InvalidTransition, match="recovery_test_policy_changed"):
        tests.run(
            run_id, task_id, RepositoryTestConfig(required_commands=("pytest",)), recovery_id=parent
        )
    tests.run(run_id, task_id, CONFIG, recovery_id=parent)
    attempt, _ = records.claim_debug(parent, None)
    records.finish_debug(parent, attempt, {"error": "llm_unavailable"}, False, None)
    # Reconstructing the service does not reset the persisted budget.
    with pytest.raises(InvalidTransition, match="debug_attempt_unavailable"):
        RecoveryRecords(factory).claim_debug(parent, None)
    records.finish(parent, False, "debug_attempts_exhausted")


@pytest.mark.asyncio
async def test_infrastructure_error_stops_without_debugging(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path
) -> None:
    run_id, task_id, context, _ = setup_run(factory, tmp_path, local_repository)
    settings = Settings(workspace_root=tmp_path)
    provider = FakeLLMProvider("unused")

    class BrokenSandbox:
        def execute(self, request: SandboxRequest) -> SandboxResult:
            raise RuntimeError("synthetic-sensitive-error")

    coding = CodingService(
        settings,
        CodingAgent(FakeLLMProvider(change().model_dump_json())),
        CodingRecords(factory),
        CodePatchService(tmp_path),
    )
    service = RecoveryService(
        settings,
        coding,
        ValidationService(settings, BrokenSandbox(), Records(factory)),
        DebugAgent(provider),
        RecoveryRecords(factory),
    )
    with pytest.raises(RecoveryError, match="^recovery_operation_failed$"):
        await service.run(run_id, task_id, context, CONFIG)
    assert provider.calls == []
    with session_scope(factory) as session:
        task = session.get(TaskExecution, task_id)
        assert task is not None and task.status == "failed"
        records = list(session.scalars(select(AgentRun)))
        assert all("synthetic-sensitive-error" not in str(a.output_metadata) for a in records)
        assert all(a.status != "running" for a in records)
