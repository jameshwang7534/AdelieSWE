"""Step 19 audit: real temporary Git repositories and DB, no external LLM calls."""

import asyncio
import os
import shutil
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.coding import CodingAgent
from app.core.config import Settings
from app.db.session import session_scope
from app.integrations.git.patches import LocalPatchGit
from app.integrations.llm.fake_provider import FakeLLMProvider
from app.integrations.llm.openai_provider import OpenAICompatibleLLMProvider
from app.models import AgentRun, TaskExecution
from app.services.code_patches import CodePatchService
from app.services.coding import CodingError, CodingService
from app.services.coding_records import CodingRecords
from tests.integration.test_coding import setup_run
from tests.integration.test_database import engine as engine
from tests.integration.test_database import factory as factory
from tests.unit.test_code_patches import EDIT, NEW, change

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Requires PostgreSQL"
)


def snapshot(path: Path) -> dict[str, bytes]:
    # Includes Git metadata and any ignored files, so failures cannot hide edits there.
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in path.rglob("*")
        if item.is_file()
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("new_file", [False, True], ids=["modification", "new-file"])
async def test_exact_diff(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path, new_file: bool
) -> None:
    run_id, task_id, context, workspace = setup_run(factory, tmp_path, local_repository)
    expected = tmp_path / "expected"
    shutil.copytree(local_repository, expected)
    name, content = ("new.py", "answer = 42\n") if new_file else ("hello.txt", "changed\n")
    (expected / name).write_text(content, encoding="utf-8", newline="\n")
    git = LocalPatchGit()
    assert git.run(["add", "--intent-to-add", "--", name], expected).code == 0
    expected_diff = git.run(["diff", "--no-ext-diff", "--no-textconv", "HEAD"], expected)
    assert expected_diff.code == 0 and expected_diff.stdout
    proposal = change(NEW, (name,)) if new_file else change()
    service = CodingService(
        Settings(workspace_root=tmp_path),
        CodingAgent(FakeLLMProvider(proposal.model_dump_json())),
        CodingRecords(factory),
        CodePatchService(tmp_path),
    )
    await service.run(run_id, task_id, context)
    assert {k: v for k, v in snapshot(workspace).items() if not k.startswith(".git/")} == {
        k: v for k, v in snapshot(expected).items() if not k.startswith(".git/")
    }
    with session_scope(factory) as session:
        agent = session.scalar(select(AgentRun))
        assert agent is not None and agent.status == "completed"
        assert agent.output_metadata["git_diff"] == expected_diff.stdout
        task = session.get(TaskExecution, task_id)
        assert task is not None and task.status == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output,code",
    [
        (change("not a patch").model_dump_json(), "unsupported_patch_header"),
        (
            change(
                NEW + EDIT.replace("-original", "-wrong"), ("new.py", "hello.txt")
            ).model_dump_json(),
            "patch_check_failed",
        ),
        (
            change(EDIT.replace("hello.txt", "../outside"), ("../outside",)).model_dump_json(),
            "unsafe_patch_path",
        ),
        (
            change(EDIT.replace("hello.txt", "/absolute"), ("/absolute",)).model_dump_json(),
            "unsafe_patch_path",
        ),
        (
            change(EDIT.replace("hello.txt", ".git/config"), (".git/config",)).model_dump_json(),
            "protected_patch_path",
        ),
        ('{"summary": 123}', "llm_invalid_response"),
        ("not JSON", "llm_invalid_response"),
    ],
    ids=[
        "malformed-patch",
        "conflicting-multifile",
        "traversal",
        "absolute",
        "git-internals",
        "invalid-schema",
        "invalid-json",
    ],
)
async def test_rejected_proposal_preserves_entire_workspace(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path, output: str, code: str
) -> None:
    run_id, task_id, context, workspace = setup_run(factory, tmp_path, local_repository)
    before = snapshot(workspace)
    service = CodingService(
        Settings(workspace_root=tmp_path),
        CodingAgent(FakeLLMProvider(output)),
        CodingRecords(factory),
        CodePatchService(tmp_path),
    )
    with pytest.raises(CodingError, match=code):
        await service.run(run_id, task_id, context)
    assert snapshot(workspace) == before
    assert not workspace.with_name(f".{workspace.name}.coding.lock").exists()
    with session_scope(factory) as session:
        agents = list(session.scalars(select(AgentRun)))
        assert len(agents) == 1
        agent = agents[0]
        assert agent.task_execution_id == task_id and agent.execution_run_id == run_id
        assert agent.status == "failed" and agent.completed_at is not None
        assert agent.output_metadata["error_code"] == code
        task = session.get(TaskExecution, task_id)
        assert task is not None and task.status == "failed"


@pytest.mark.asyncio
async def test_llm_timeout_records_failure_without_edits(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path
) -> None:
    run_id, task_id, context, workspace = setup_run(factory, tmp_path, local_repository)
    before = snapshot(workspace)
    calls = 0

    async def stalled(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()
        raise AssertionError("Provider timeout must cancel stalled transport")

    async with httpx.AsyncClient(
        base_url="https://llm.example.invalid/", transport=httpx.MockTransport(stalled)
    ) as client:
        provider = OpenAICompatibleLLMProvider(client, "test-model", timeout=0.02, max_retries=0)
        service = CodingService(
            Settings(workspace_root=tmp_path),
            CodingAgent(provider),
            CodingRecords(factory),
            CodePatchService(tmp_path),
        )
        with pytest.raises(CodingError, match="llm_timeout"):
            await asyncio.wait_for(service.run(run_id, task_id, context), timeout=5)
    assert calls == 1 and snapshot(workspace) == before
    assert not workspace.with_name(f".{workspace.name}.coding.lock").exists()
    with session_scope(factory) as session:
        agent = session.scalar(select(AgentRun))
        assert agent is not None and agent.status == "failed"
        assert agent.output_metadata["error_code"] == "llm_timeout"
        assert agent.completed_at is not None
        task = session.get(TaskExecution, task_id)
        assert task is not None and task.status == "failed"
