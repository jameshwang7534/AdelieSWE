"""Fake LLM -> real local Git patch -> PostgreSQL agent/task records, no test execution."""

import json
import os
import shutil
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.coding import CodingAgent
from app.core.config import Settings
from app.db.session import session_scope
from app.integrations.llm.fake_provider import FakeLLMProvider
from app.integrations.llm.provider import TokenUsage
from app.models import AgentRun, ExecutionRun, ImplementationPlan, TaskExecution
from app.orchestration.state import InvalidTransition
from app.schemas.context import ContextSnippet, IssueContext
from app.services.code_patches import CodePatchService
from app.services.coding import CodingError, CodingService
from app.services.coding_records import CodingRecords
from app.services.context_store import PostgresContextStore
from app.services.executions import ExecutionService
from tests.integration.test_database import engine as engine
from tests.integration.test_database import factory as factory
from tests.integration.test_executions import make_plan
from tests.unit.test_code_patches import EDIT, change

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Requires PostgreSQL"
)


def setup_run(
    factory: sessionmaker[Session], root: Path, source: Path
) -> tuple[UUID, UUID, IssueContext, Path]:
    scheduler = ExecutionService(factory)
    run_id = scheduler.create(make_plan(factory))
    ready = scheduler.reconcile(run_id)
    with session_scope(factory) as session:
        plan = session.get(ImplementationPlan, ready.plan_id)
        assert plan is not None
        issue_id = plan.issue_id
    seed = PostgresContextStore(factory).load_issue(issue_id, 12000)
    context = IssueContext(
        **seed.model_dump(), queries=[], relevant_files=[], snippets=[], code_chars=0, limited=False
    )
    workspace = root / "executions" / str(context.repository.id) / str(run_id)
    shutil.copytree(source, workspace)
    return run_id, ready.tasks[0].id, context, workspace


@pytest.mark.asyncio
async def test_coding_persists_but_does_not_complete(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path
) -> None:
    run_id, task_id, context, workspace = setup_run(factory, tmp_path, local_repository)
    context.snippets = [
        ContextSnippet(
            chunk_id=uuid4(),
            file_path="hello.txt",
            start_line=1,
            end_line=1,
            content="original\n",
            retrieval=[],
        )
    ]
    context.relevant_files = ["hello.txt"]
    context.code_chars = len("original\n")
    provider = FakeLLMProvider(
        change().model_dump_json(), TokenUsage(input_tokens=12, output_tokens=30)
    )
    service = CodingService(
        Settings(workspace_root=tmp_path, llm_api_key=None, github_token=None),
        CodingAgent(provider),
        CodingRecords(factory),
        CodePatchService(tmp_path),
    )
    assert await service.run(run_id, task_id, context) == change()
    assert (workspace / "hello.txt").read_text() == "changed\n"
    sent = json.loads(provider.calls[0][1].content)
    assert sent["task"]["task_key"] == "A" and sent["workspace_status"] == "clean"
    assert sent["context"]["issue"]["id"] == str(context.issue.id)
    assert sent["context"]["snippets"][0]["content"] == "original\n"
    assert sent["dependency_outcomes"] == []
    with session_scope(factory) as session:
        task_record = session.get(TaskExecution, task_id)
        run = session.get(ExecutionRun, run_id)
        agent = session.scalar(select(AgentRun).where(AgentRun.task_execution_id == task_id))
        assert task_record is not None and task_record.status == "running"
        assert task_record.output_summary == "patch_applied_awaiting_validation"
        assert run is not None and run.status == "running"
        assert agent is not None and agent.status == "completed" and agent.completed_at is not None
        assert agent.input_tokens == 12 and agent.output_tokens == 30
        assert agent.output_metadata["validation"] == "not_run"
        assert "+changed" in str(agent.output_metadata["git_diff"])
    with pytest.raises(InvalidTransition, match="coding_task_not_ready"):
        await service.run(run_id, task_id, context)
    assert len(provider.calls) == 1
    # C depends on A and B; applying A's patch does not release it.
    assert ExecutionService(factory).get(run_id).tasks[2].status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output,code",
    [
        ("not json", "llm_invalid_response"),
        (
            change(EDIT.replace("-original", "-wrong-context")).model_dump_json(),
            "patch_check_failed",
        ),
        (
            change(
                EDIT.replace("hello.txt", "../outside.txt"), ("../outside.txt",)
            ).model_dump_json(),
            "unsafe_patch_path",
        ),
    ],
)
async def test_coding_failure_is_persisted(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path, output: str, code: str
) -> None:
    run_id, task_id, context, workspace = setup_run(factory, tmp_path, local_repository)
    service = CodingService(
        Settings(workspace_root=tmp_path, llm_api_key=None, github_token=None),
        CodingAgent(FakeLLMProvider(output)),
        CodingRecords(factory),
        CodePatchService(tmp_path),
    )
    with pytest.raises(CodingError, match=code):
        await service.run(run_id, task_id, context)
    assert (workspace / "hello.txt").read_text() == "original\n"
    with session_scope(factory) as session:
        agent = session.scalar(select(AgentRun).where(AgentRun.task_execution_id == task_id))
        assert agent is not None and agent.status == "failed" and agent.completed_at is not None
        assert agent.output_metadata["error_code"] == code
        if code == "patch_check_failed":
            assert "patch" in str(agent.output_metadata["diagnostic"])
        task_record = session.get(TaskExecution, task_id)
        assert task_record is not None and task_record.status == "failed"
    states = ExecutionService(factory).get(run_id).tasks
    assert states[2].status == states[3].status == "blocked"


@pytest.mark.asyncio
async def test_dependency_outcomes_in_prompt(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path
) -> None:
    run_id, _, context, _ = setup_run(factory, tmp_path, local_repository)
    scheduler = ExecutionService(factory)
    for task_record in scheduler.get(run_id).tasks[:2]:
        scheduler.transition(run_id, task_record.id, "running")
        scheduler.transition(
            run_id, task_record.id, "completed", f"Verified {task_record.task_key}"
        )
    next_task = scheduler.get(run_id).tasks[2]
    provider = FakeLLMProvider(change().model_dump_json())
    service = CodingService(
        Settings(workspace_root=tmp_path, llm_api_key=None, github_token=None),
        CodingAgent(provider),
        CodingRecords(factory),
        CodePatchService(tmp_path),
    )
    await service.run(run_id, next_task.id, context)
    inputs = json.loads(provider.calls[0][1].content)
    assert inputs["task"]["task_key"] == "C"
    assert inputs["dependency_outcomes"] == [
        {"task_key": "A", "status": "completed", "summary": "Verified A"},
        {"task_key": "B", "status": "completed", "summary": "Verified B"},
    ]


@pytest.mark.asyncio
async def test_cross_repository_context_rejected_before_claim(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path
) -> None:
    run_id, task_id, context, _ = setup_run(factory, tmp_path, local_repository)
    context.repository.id = uuid4()
    provider = FakeLLMProvider(change().model_dump_json())
    service = CodingService(
        Settings(workspace_root=tmp_path),
        CodingAgent(provider),
        CodingRecords(factory),
        CodePatchService(tmp_path),
    )
    with pytest.raises(InvalidTransition, match="coding_context_mismatch"):
        await service.run(run_id, task_id, context)
    assert provider.calls == []
    with session_scope(factory) as session:
        assert session.scalar(select(AgentRun)) is None
        task = session.get(TaskExecution, task_id)
        assert task is not None and task.status == "queued"


@pytest.mark.asyncio
async def test_configured_secret_in_proposal_is_not_applied_or_logged(
    factory: sessionmaker[Session],
    tmp_path: Path,
    local_repository: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from pydantic import SecretStr

    run_id, task_id, context, workspace = setup_run(factory, tmp_path, local_repository)
    sentinel = "synthetic-test-credential-not-a-real-key"
    provider = FakeLLMProvider(change(EDIT.replace("+changed", f"+{sentinel}")).model_dump_json())
    service = CodingService(
        Settings(workspace_root=tmp_path, llm_api_key=SecretStr(sentinel)),
        CodingAgent(provider),
        CodingRecords(factory),
        CodePatchService(tmp_path),
    )
    with pytest.raises(CodingError, match="secret_in_code_proposal"):
        await service.run(run_id, task_id, context)
    assert (workspace / "hello.txt").read_text() == "original\n"
    with session_scope(factory) as session:
        agent = session.scalar(select(AgentRun))
        assert agent is not None and sentinel not in json.dumps(agent.output_metadata)
    assert sentinel not in caplog.text
