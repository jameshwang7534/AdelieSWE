"""Failure injection across a real persisted workflow; external providers are fixtures."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.integrations.git.runner import SubprocessGitRunner
from app.models import AgentRun, ExecutionRun, ImplementationPlan, PullRequest, TaskExecution
from app.orchestration.workflow import WorkflowEngine
from app.orchestration.workflow_records import WorkflowClaim, WorkflowRecords
from app.orchestration.workflow_stages import StageResult
from app.services.executions import ExecutionService
from tests.integration.test_database import engine as engine
from tests.integration.test_database import factory as factory
from tests.integration.test_executions import make_plan
from tests.integration.test_workflow import FixtureWorkflow

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Requires PostgreSQL"
)


@pytest.mark.asyncio
async def test_failures_restarts_and_duplicate_side_effects(
    factory: sessionmaker[Session],
    tmp_path: Path,
    local_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(workspace_root=tmp_path)
    fixture = FixtureWorkflow(factory, tmp_path, local_repository, settings)
    injected: set[str] = set()
    original_finish = WorkflowRecords.finish

    def checkpoint(
        self: WorkflowRecords,
        claim: WorkflowClaim,
        data: dict[str, object],
        *,
        repeat: bool = False,
        error: str | None = None,
        blocked: bool = False,
        retry_seconds: int | None = None,
    ) -> None:
        original_finish(
            self,
            claim,
            data,
            repeat=repeat,
            error=error,
            blocked=blocked,
            retry_seconds=retry_seconds,
        )
        if claim.stage == "plan" and "after_checkpoint" not in injected:
            injected.add("after_checkpoint")
            raise RuntimeError("injected_after_checkpoint")

    class InterruptedStages:
        async def run(self, claim: WorkflowClaim) -> StageResult:
            if claim.stage == "context" and "before_stage_update" not in injected:
                injected.add("before_stage_update")
                raise RuntimeError("injected_before_stage_update")
            result = await fixture.stages.run(claim)
            if claim.stage == "execution" and "after_execution_update" not in injected:
                injected.add("after_execution_update")
                raise RuntimeError("injected_after_execution_update")
            return result

    monkeypatch.setattr(WorkflowRecords, "finish", checkpoint)
    status = fixture.records.start(fixture.request())
    duplicates = 0
    try:
        for _ in range(30):
            # Fresh service instances reload exclusively from persisted state.
            records = WorkflowRecords(factory, settings)
            before = records.get(status.id)
            runtime = WorkflowEngine(records, InterruptedStages())
            try:
                status = await runtime.advance(before.id, before.stage, before.generation)
            except RuntimeError as error:
                assert str(error) == "injected_after_checkpoint"
                status = records.get(before.id)
                assert status.stage == "execution" and status.status == "pending"
            replay = WorkflowEngine(WorkflowRecords(factory, settings), InterruptedStages())
            repeated = await replay.advance(before.id, before.stage, before.generation)
            assert not replay.did_advance and repeated == status
            duplicates += 1
            if status.status == "failed":
                status = records.resume(status.id)
            if status.status == "completed":
                break
        assert status.status == "completed"
        assert injected == {"before_stage_update", "after_execution_update", "after_checkpoint"}
        assert status.execution_id is not None
        execution = ExecutionService(factory)
        for task in execution.get(status.execution_id).tasks:
            assert not execution.transition(status.execution_id, task.id, "running")
        assert execution.reconcile(status.execution_id).status == "completed"
        with factory() as session:
            counts = {
                model.__name__: session.scalar(select(func.count()).select_from(model))
                for model in (
                    AgentRun,
                    TaskExecution,
                    ImplementationPlan,
                    ExecutionRun,
                    PullRequest,
                )
            }
        assert counts == {
            "AgentRun": 10,
            "TaskExecution": 2,
            "ImplementationPlan": 1,
            "ExecutionRun": 1,
            "PullRequest": 1,
        }
        assert len(fixture.provider.calls) == 5
        assert fixture.pushes == len(fixture.prs) == 1
        workspace = tmp_path / "executions" / str(status.repository_id) / str(status.execution_id)
        git = SubprocessGitRunner()
        refs = git.run(
            ["for-each-ref", "--format=%(refname)", "refs/heads/ai-platform/"], cwd=workspace
        ).splitlines()
        assert len(refs) == 1
        commits = git.run(["rev-list", "--count", f"HEAD..{refs[0]}"], cwd=workspace).strip()
        assert commits == "1"
        print(
            "STEP25_AUDIT="
            + json.dumps(
                {
                    "injections": sorted(injected),
                    "duplicate_deliveries": duplicates,
                    "counts": counts,
                    "branches": len(refs),
                    "commits": int(commits),
                    "status": status.status,
                }
            )
        )
    finally:
        fixture.client.close()


def test_execution_reloaded_in_new_process(engine: Engine) -> None:
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = ExecutionService(sessions)
    identifier = service.create(make_plan(sessions, diamond=True))
    state = service.reconcile(identifier)
    script = """
import json, os, sys
from uuid import UUID
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.services.executions import ExecutionService
engine = create_engine(os.environ['AUDIT_DATABASE_URL'], hide_parameters=True)
try:
    state = ExecutionService(sessionmaker(engine)).get(UUID(sys.argv[1]))
    print(json.dumps({'pid': os.getpid(), 'states': [t.status for t in state.tasks]}))
finally:
    engine.dispose()
"""
    environment = {
        **os.environ,
        "AUDIT_DATABASE_URL": engine.url.render_as_string(hide_password=False),
    }

    def reload() -> dict[str, object]:
        result = subprocess.run(
            [sys.executable, "-c", script, str(identifier)],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        data: dict[str, object] = json.loads(result.stdout)
        return data

    first = reload()
    assert first["states"] == ["queued", "pending", "pending", "pending"]
    service.transition(identifier, state.tasks[0].id, "running")
    service.transition(identifier, state.tasks[0].id, "completed")
    second = reload()
    assert second["states"] == ["completed", "queued", "queued", "pending"]
    assert first["pid"] != second["pid"]
    assert not service.transition(identifier, state.tasks[0].id, "running")
    with sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(TaskExecution)
                .where(TaskExecution.execution_run_id == identifier)
            )
            == 4
        )
    print("STEP25_PROCESS_RELOAD=" + json.dumps({"before": first, "after": second}))
