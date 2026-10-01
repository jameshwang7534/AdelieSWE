"""Real completion/test provenance and Git diff, fake advisory reviewer, no GitHub calls."""

import asyncio
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.coding import CodingAgent
from app.agents.review import ReviewAgent
from app.core.config import Settings
from app.db.session import session_scope
from app.integrations.llm.fake_provider import FakeLLMProvider
from app.integrations.llm.provider import LLMResult
from app.models import AgentRun, PullRequest, TaskExecution
from app.sandbox.base import SandboxResult
from app.schemas.review import ReviewDecision, ReviewInput, ReviewResult
from app.schemas.testing import RepositoryTestConfig
from app.services.code_patches import CodePatchService
from app.services.coding import CodingService
from app.services.coding_records import CodingRecords
from app.services.executions import ExecutionService
from app.services.review import ReviewError, ReviewService
from app.services.review_records import ReviewRecords
from app.services.test_records import TestRecords as Records
from app.services.testing import TestService as ValidationService
from tests.integration.test_coding import setup_run
from tests.integration.test_database import engine as engine
from tests.integration.test_database import factory as factory
from tests.integration.test_test_agent import FakeSandbox
from tests.unit.test_code_patches import EDIT, change

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Requires PostgreSQL"
)
CONFIG = RepositoryTestConfig(required_commands=("python -m unittest",))
APPROVAL = {"summary": "Issue implemented with passing checks", "approved": True, "findings": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "approved",
        "blocking",
        "rejected",
        "malformed",
        "stale",
        "missing-test",
        "failed-test",
        "missing-command",
        "unfinished",
        "changed-during-review",
        "evidence-changed",
        "timeout",
    ],
)
async def test_review_gate_and_persistence(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path, case: str
) -> None:
    run_id, _, context, workspace = setup_run(factory, tmp_path, local_repository)
    settings = Settings(workspace_root=tmp_path, llm_timeout_seconds=1)
    scheduler = ExecutionService(factory)
    before = "original"
    for task in scheduler.get(run_id).tasks:
        patch = EDIT.replace("-original", f"-{before}").replace("+changed", f"+{task.task_key}")
        await CodingService(
            settings,
            CodingAgent(FakeLLMProvider(change(patch).model_dump_json())),
            CodingRecords(factory),
            CodePatchService(tmp_path),
        ).run(run_id, task.id, context)
        sandbox = FakeSandbox(
            [
                SandboxResult(
                    exit_code=0,
                    stdout=f"tested {task.task_key}",
                    stderr="",
                    elapsed_seconds=0.1,
                    timed_out=False,
                    output_truncated=False,
                )
            ]
        )
        ValidationService(settings, sandbox, Records(factory)).run(run_id, task.id, CONFIG)
        before = task.task_key
    assert scheduler.get(run_id).status == "completed"
    if case == "stale":
        (workspace / "hello.txt").write_bytes(b"changed after tests\n")
    with session_scope(factory) as session:
        test = session.scalars(
            select(AgentRun).where(AgentRun.agent_type == "test").order_by(AgentRun.started_at)
        ).first()
        assert test is not None
        if case == "missing-test":
            session.delete(test)
        if case in {"failed-test", "missing-command"}:
            metadata = dict(test.output_metadata)
            report = json.loads(json.dumps(metadata["report"]))
            if case == "failed-test":
                # Claiming passed is not enough: check the actual exit code.
                report["results"][0]["exit_code"] = 1
            else:
                report["results"] = []
            metadata["report"] = report
            test.output_metadata = metadata
        if case == "unfinished":
            task_record = session.get(TaskExecution, test.task_execution_id)
            assert task_record is not None
            task_record.status = "running"
    output = dict(APPROVAL)
    if case == "blocking":
        output["findings"] = [
            {
                "severity": "blocking",
                "file_path": "hello.txt",
                "line": 1,
                "description": "Likely bug",
                "recommendation": "Correct behavior",
                "reference": None,
            }
        ]
    if case == "rejected":
        output["approved"] = False
    provider = FakeLLMProvider("invalid JSON" if case == "malformed" else json.dumps(output))

    class MutatingReviewer(ReviewAgent):
        async def review(self, inputs: ReviewInput) -> LLMResult[ReviewResult]:
            if case == "timeout":
                await asyncio.Event().wait()
            result = await super().review(inputs)
            if case == "changed-during-review":
                (workspace / "hello.txt").write_bytes(b"concurrent change\n")
            else:
                with session_scope(factory) as session:
                    task_record = session.scalar(select(TaskExecution))
                    assert task_record is not None
                    task_record.status = "running"
            return result

    agent = (
        MutatingReviewer(provider)
        if case in {"changed-during-review", "evidence-changed", "timeout"}
        else ReviewAgent(provider)
    )
    service = ReviewService(settings, agent, ReviewRecords(factory), CodePatchService(tmp_path))
    if case in {"malformed", "timeout"}:
        with pytest.raises(
            ReviewError, match="llm_timeout" if case == "timeout" else "llm_invalid_response"
        ):
            await service.run(run_id, context, CONFIG)
    else:
        decision = await service.run(run_id, context, CONFIG)
        assert decision.approved == (case == "approved")
        if case in {"stale", "missing-test", "failed-test", "missing-command", "unfinished"}:
            assert decision.mechanical_errors and provider.calls == []
        elif case in {"approved", "blocking", "rejected"}:
            assert not decision.mechanical_errors and decision.review is not None
            assert decision.review.approved == (case != "rejected")
        if case == "changed-during-review":
            assert "workspace_changed_during_review" in decision.mechanical_errors
        if case == "evidence-changed":
            assert "execution_changed_during_review" in decision.mechanical_errors
    with session_scope(factory) as session:
        review = session.scalar(select(AgentRun).where(AgentRun.agent_type == "review"))
        assert review is not None and review.completed_at is not None
        assert review.status == ("failed" if case in {"malformed", "timeout"} else "completed")
        persisted = ReviewDecision.model_validate(review.output_metadata["decision"])
        assert persisted.approved == (case == "approved")
        assert session.scalar(select(func.count()).select_from(PullRequest)) == 0
    if case == "approved":
        sent = json.loads(provider.calls[0][1].content)
        assert sent["context"]["issue"]["id"] == str(context.issue.id)
        assert (
            len(sent["plan"]["tasks"])
            == len(sent["task_outcomes"])
            == len(sent["test_results"])
            == 4
        )
        assert "-original" in sent["full_diff"] and "+D" in sent["full_diff"]
