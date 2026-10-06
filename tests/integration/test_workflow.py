"""Full stage composition with real Git/Postgres and deterministic external providers."""

import difflib
import json
import os
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import UUID, uuid4

import httpx
import pytest
from celery.contrib.testing.worker import start_worker
from fastapi.testclient import TestClient
from kombu import Queue
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.session import session_scope
from app.integrations.git.runner import SubprocessGitRunner
from app.integrations.github.client import HttpGitHubClient
from app.integrations.llm.embeddings import EmbeddingProvider
from app.integrations.llm.fake_embeddings import FakeEmbeddingProvider
from app.integrations.llm.fake_provider import FakeLLMProvider
from app.integrations.llm.provider import LLMProvider
from app.main import create_app
from app.models import (
    AgentRun,
    CodeChunk,
    ExecutionRun,
    ImplementationPlan,
    PullRequest,
    WorkflowRun,
)
from app.orchestration.workflow import WorkflowEngine
from app.orchestration.workflow_records import STAGES, WorkflowError, WorkflowRecords
from app.orchestration.workflow_stages import WorkflowStages
from app.pull_requests.contracts import Publication
from app.pull_requests.git import LocalPublicationGit
from app.sandbox.base import Sandbox, SandboxRequest, SandboxResult
from app.sandbox.docker import DockerSandbox
from app.schemas.coding import CodeChangeProposal
from app.schemas.context import IssueContext
from app.schemas.debugging import RecoveryReport
from app.schemas.testing import RepositoryTestConfig
from app.schemas.workflows import WorkflowRequest, WorkflowStatus
from app.services.executions import ExecutionService
from app.services.recovery import RecoveryService
from app.services.workflow_resources import CeleryWorkflowQueue, WorkflowQueue
from app.services.workspace import WorkspaceService
from app.workers.factory import create_celery_app
from app.workers.workflow import ADVANCE_WORKFLOW
from tests.integration.test_database import engine as engine
from tests.integration.test_database import factory as factory

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Requires PostgreSQL"
)


def patch_json(path: str, old: str, new: str) -> str:
    diff = f"diff --git a/{path} b/{path}\n" + "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    return CodeChangeProposal(
        summary="Focused fix",
        files_changed=(path,),
        unified_diff=diff,
        assumptions=(),
        tests_to_run=("python -m unittest",),
    ).model_dump_json()


class FixtureSandbox:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, request: SandboxRequest) -> SandboxResult:
        self.calls += 1
        assert request.command == ("python", "-m", "unittest", "discover")
        correct = "return 2" in (request.workspace / "calc.py").read_text()
        return SandboxResult(
            exit_code=0 if correct else 1,
            stdout="Ran 1 test",
            stderr="" if correct else "AssertionError: 3 != 2",
            elapsed_seconds=0.1,
            timed_out=False,
            output_truncated=False,
        )


class FixtureWorkflow:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        root: Path,
        source: Path,
        settings: Settings,
        sandbox: Sandbox | None = None,
    ) -> None:
        self.settings, self.sessions = settings, sessions
        self.owner, self.repo = "fixture", f"repo-{uuid4().hex}"
        self.prs: list[dict[str, Any]] = []
        self.remote_sha: str | None = None
        self.pushes = 0
        (source / "calc.py").write_bytes(b"def answer():\n    return 1\n")
        (source / "test_calc.py").write_bytes(
            b"import unittest\nfrom calc import answer\n\nclass TestAnswer(unittest.TestCase):\n"
            b"    def test_answer(self):\n        self.assertEqual(answer(), 2)\n"
        )
        (source / "README.md").write_bytes(b"# Fixture\n")
        with (source / ".gitignore").open("a", encoding="utf-8") as stream:
            stream.write("__pycache__/\n*.pyc\n")
        git = SubprocessGitRunner()
        git.run(["add", "."], cwd=source)
        git.run(
            [
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "Add fixture code",
            ],
            cwd=source,
        )
        tasks = [
            {
                "task_key": key,
                "title": title,
                "description": title,
                "rationale": "Resolve issue",
                "target_files": [path],
                "dependencies": dependencies,
                "acceptance_criteria": ["Tests pass"],
                "suggested_tests": ["python -m unittest"],
            }
            for key, title, path, dependencies in [
                ("A", "Fix answer", "calc.py", []),
                ("B", "Document answer", "README.md", ["A"]),
            ]
        ]
        self.provider = FakeLLMProvider(
            [
                json.dumps({"summary": "Fix answer and document", "tasks": tasks}),
                patch_json(
                    "calc.py", "def answer():\n    return 1\n", "def answer():\n    return 3\n"
                ),
                patch_json(
                    "calc.py", "def answer():\n    return 3\n", "def answer():\n    return 2\n"
                ),
                patch_json("README.md", "# Fixture\n", "# Fixture\n\nFixed answer.\n"),
                json.dumps(
                    {
                        "summary": "Issue satisfied and tests passed",
                        "approved": True,
                        "findings": [],
                    }
                ),
            ]
        )
        self.sandbox = sandbox or FixtureSandbox()
        self.client = httpx.Client(
            base_url="https://api.github.com/", transport=httpx.MockTransport(self.http)
        )
        outer = self

        class LocalWorkspace(WorkspaceService):
            def prepare(
                self,
                repository_id: UUID,
                source_url: str,
                default_branch: str,
                on_status: Any = None,
            ) -> Any:
                return super().prepare(repository_id, str(source), default_branch, on_status)

        class MockPush(LocalPublicationGit):
            def push(self, workspace: Path, publication: Publication) -> None:
                outer.pushes += 1
                outer.remote_sha = publication.commit

        @asynccontextmanager
        async def llm() -> AsyncIterator[LLMProvider]:
            yield self.provider

        @contextmanager
        def embeddings() -> Iterator[EmbeddingProvider]:
            yield FakeEmbeddingProvider()

        github = HttpGitHubClient(self.client)
        self.stages = WorkflowStages(
            settings,
            sessions,
            github,
            github,
            LocalWorkspace(root, git, allow_local=True),
            llm,
            embeddings,
            self.sandbox,
            MockPush(settings),
        )
        self.records = WorkflowRecords(sessions, settings)

    def request(self, publish: bool = True) -> WorkflowRequest:
        return WorkflowRequest(
            request_id=uuid4(),
            github_owner=self.owner,
            github_name=self.repo,
            issue_number=1,
            publish_pull_request=publish,
        )

    def http(self, request: httpx.Request) -> httpx.Response:
        repo = f"{self.owner}/{self.repo}"
        if "/git/ref/" in request.url.path:
            return httpx.Response(
                200 if self.remote_sha else 404, json={"object": {"sha": self.remote_sha}}
            )
        if request.url.path.endswith("/pulls"):
            if request.method == "GET":
                return httpx.Response(200, json=self.prs)
            body = json.loads(request.content)
            data = {
                "number": 1,
                "html_url": f"https://github.com/{repo}/pull/1",
                "state": "open",
                "body": body["body"],
                "head": {"ref": body["head"], "sha": self.remote_sha, "repo": {"full_name": repo}},
                "base": {"ref": body["base"], "repo": {"full_name": repo}},
            }
            self.prs.append(data)
            return httpx.Response(201, json=data)
        if "/issues/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "number": 1,
                    "title": "Fix answer in calc.py",
                    "body": "answer must return 2; update README",
                    "state": "open",
                    "html_url": f"https://github.com/{repo}/issues/1",
                },
            )
        return httpx.Response(
            200,
            json={
                "owner": {"login": self.owner},
                "name": self.repo,
                "clone_url": f"https://github.com/{repo}.git",
                "default_branch": "main",
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("publish", [True, False])
async def test_full_workflow(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path, publish: bool
) -> None:
    fixture = FixtureWorkflow(
        factory, tmp_path, local_repository, Settings(workspace_root=tmp_path)
    )
    request = fixture.request(publish)
    try:
        status = fixture.records.start(request)
        assert fixture.records.start(request).id == status.id
        for _ in range(25):
            # A newly constructed engine on every step proves there is no in-memory workflow state.
            status = await WorkflowEngine(fixture.records, fixture.stages).advance(status.id)
            if status.status != "pending":
                break
        assert status.status == "completed", status.model_dump()
        assert status.execution_id is not None
        assert ExecutionService(factory).get(status.execution_id).status == "completed"
        assert fixture.pushes == len(fixture.prs) == int(publish)
        assert len(fixture.provider.calls) == 5
        planner_input = json.loads(fixture.provider.calls[0][1].content.split("\n", 1)[1])
        assert planner_input["snippets"]
        assert any(
            e["vector_rank"] is not None for s in planner_input["snippets"] for e in s["retrieval"]
        )
        assert "calc.py" in fixture.provider.calls[0][1].content
        assert {item["stage"] for item in status.history} >= set(STAGES[:-2])
        assert (
            await WorkflowEngine(fixture.records, fixture.stages).advance(status.id)
        ).status == "completed"
        assert fixture.pushes == int(publish)
        with session_scope(factory) as session:
            assert session.scalar(
                select(func.count()).select_from(CodeChunk).where(CodeChunk.embedding.is_not(None))
            )
            assert session.scalar(select(func.count()).select_from(ImplementationPlan)) == 1
            assert session.scalar(select(func.count()).select_from(ExecutionRun)) == 1
            assert session.scalar(select(func.count()).select_from(PullRequest)) == int(publish)
            assert (
                session.scalar(
                    select(func.count()).select_from(AgentRun).where(AgentRun.agent_type == "debug")
                )
                == 1
            )
            assert (
                session.scalar(
                    select(func.count()).select_from(AgentRun).where(AgentRun.agent_type == "test")
                )
                == 3
            )
    finally:
        fixture.client.close()


def test_claim_resume_and_fencing(factory: sessionmaker[Session]) -> None:
    settings = Settings(workflow_stage_attempts=2)
    records = WorkflowRecords(factory, settings)
    request = WorkflowRequest(
        request_id=uuid4(), github_owner="fixture", github_name="repo", issue_number=1
    )
    status = records.start(request)
    with pytest.raises(WorkflowError, match="idempotency_conflict"):
        records.start(request.model_copy(update={"issue_number": 2}))
    claim = records.claim(status.id)
    assert claim is not None and records.claim(status.id) is None
    records.finish(claim, {}, error="temporary_failure")
    assert records.resume(status.id).status == "pending"
    assert records.claim(status.id, expected_stage="publish") is None
    second = records.claim(status.id)
    assert second is not None
    records.finish(second, {}, error="temporary_failure")
    with pytest.raises(WorkflowError, match="resume_not_allowed"):
        records.resume(status.id)
    other = records.start(request.model_copy(update={"request_id": uuid4()}))
    stale = records.claim(other.id)
    assert stale is not None
    with session_scope(factory) as session:
        row = session.get(WorkflowRun, other.id)
        assert row is not None
        row.stage = "implement"
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    records.recoverable()
    assert records.get(other.id).status == "blocked"
    with pytest.raises(WorkflowError, match="claim_lost"):
        records.finish(stale, {})


def test_api_broker_failure_and_recovery(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.services import workflow_resources
    from app.workers.workflow import recover_workflows

    records = WorkflowRecords(factory, Settings())
    queue = Mock(spec=WorkflowQueue)
    queue.enqueue.side_effect = RuntimeError("private broker detail")

    @contextmanager
    def resources(settings: Settings) -> Iterator[tuple[WorkflowRecords, WorkflowQueue] | None]:
        yield records, queue

    request = WorkflowRequest(
        request_id=uuid4(), github_owner="fixture", github_name="repo", issue_number=1
    )
    with TestClient(create_app(Settings(database_url=None), workflow_factory=resources)) as client:
        response = client.post("/workflows", json=request.model_dump(mode="json"))
        assert response.status_code == 202 and response.json()["status"] == "pending"
        assert "private broker detail" not in caplog.text
        assert client.get(f"/workflows/{request.request_id}").status_code == 200
        assert client.get(f"/workflows/{uuid4()}").status_code == 404
        conflict = {**request.model_dump(mode="json"), "issue_number": 2}
        assert client.post("/workflows", json=conflict).status_code == 409
        queue.enqueue.side_effect = None
        queue.reset_mock()
        monkeypatch.setattr(workflow_resources, "workflow_api_resources", resources)
        assert recover_workflows() == {"dispatched": 1}
        queue.enqueue.assert_called_once()
        claim = records.claim(request.request_id)
        assert claim is not None
        records.finish(claim, {}, error="temporary_failure")
        assert client.post(f"/workflows/{request.request_id}/resume").status_code == 202


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["plan", "execution"])
async def test_restart_after_persistence_before_checkpoint(
    factory: sessionmaker[Session],
    tmp_path: Path,
    local_repository: Path,
    stage: str,
) -> None:
    fixture = FixtureWorkflow(
        factory, tmp_path, local_repository, Settings(workspace_root=tmp_path)
    )
    status = fixture.records.start(fixture.request())
    try:
        for _ in range(12):
            if status.stage == stage:
                break
            status = await WorkflowEngine(fixture.records, fixture.stages).advance(status.id)
            assert status.status == "pending"
        claim = fixture.records.claim(status.id)
        assert claim is not None
        await fixture.stages.run(claim)
        fixture.records.finish(claim, {}, error="checkpoint_write_failed")
        fixture.records.resume(status.id)
        status = await WorkflowEngine(fixture.records, fixture.stages).advance(status.id)
        assert status.status == "pending" and status.stage != stage
        assert len(fixture.provider.calls) == 1
        with session_scope(factory) as session:
            assert session.scalar(select(func.count()).select_from(ImplementationPlan)) == 1
            if stage == "execution":
                assert session.scalar(select(func.count()).select_from(ExecutionRun)) == 1
    finally:
        fixture.client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["tests-exhausted", "review-rejected", "embedding-retry"])
async def test_workflow_failures(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path, case: str
) -> None:
    settings = Settings(
        workspace_root=tmp_path, max_recovery_attempts=0 if case == "tests-exhausted" else 3
    )
    fixture = FixtureWorkflow(factory, tmp_path, local_repository, settings)
    status = fixture.records.start(fixture.request())
    if case == "review-rejected":
        fixture.provider.responses = (
            *fixture.provider.responses[:-1],
            json.dumps(
                {
                    "summary": "Blocking bug",
                    "approved": True,
                    "findings": [
                        {"severity": "blocking", "description": "Bug", "recommendation": "Fix"}
                    ],
                }
            ),
        )
    failed_embedding = False
    embeddings = fixture.stages.embeddings
    try:
        for _ in range(25):
            if case == "embedding-retry" and status.stage == "embed" and not failed_embedding:
                fixture.stages.embeddings = Mock(
                    side_effect=RuntimeError("private provider detail")
                )
                status = await WorkflowEngine(fixture.records, fixture.stages).advance(status.id)
                assert status.status == "failed" and status.error_code == "workflow_stage_failed"
                assert "private provider detail" not in status.model_dump_json()
                fixture.stages.embeddings = embeddings
                status = fixture.records.resume(status.id)
                failed_embedding = True
            status = await WorkflowEngine(fixture.records, fixture.stages).advance(status.id)
            if status.status != "pending":
                break
        if case == "embedding-retry":
            assert status.status == "completed" and fixture.pushes == 1
        else:
            assert status.status == "blocked" and fixture.pushes == 0
            assert status.stage == ("implement" if case == "tests-exhausted" else "review")
            with pytest.raises(WorkflowError, match="resume_not_allowed"):
                fixture.records.resume(status.id)
            if case == "tests-exhausted":
                assert status.execution_id is not None
                tasks = ExecutionService(factory).get(status.execution_id).tasks
                assert [task.status for task in tasks] == ["failed", "blocked"]
    finally:
        fixture.client.close()


@pytest.mark.skipif(os.environ.get("RUN_WORKER_TESTS") != "1", reason="Requires Redis worker")
def test_api_redis_worker_workflow(
    engine: Engine,
    tmp_path: Path,
    local_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.orchestration import workflow_resources

    monkeypatch.setenv("DATABASE_URL", engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    settings = Settings()
    sessions = sessionmaker(engine, expire_on_commit=False)
    sandbox: Sandbox = (
        DockerSandbox(settings) if os.environ.get("RUN_SANDBOX_TESTS") == "1" else FixtureSandbox()
    )
    fixture = FixtureWorkflow(sessions, tmp_path, local_repository, settings, sandbox)
    transitions: list[dict[str, str]] = []
    original_recovery = RecoveryService.run

    async def traced_recovery(
        self: RecoveryService,
        run_id: UUID,
        task_id: UUID,
        context: IssueContext,
        config: RepositoryTestConfig,
    ) -> RecoveryReport:
        transitions.append(
            {t.task_key: t.status for t in ExecutionService(sessions).get(run_id).tasks}
        )
        result = await original_recovery(self, run_id, task_id, context, config)
        transitions.append(
            {t.task_key: t.status for t in ExecutionService(sessions).get(run_id).tasks}
        )
        return result

    monkeypatch.setattr(RecoveryService, "run", traced_recovery)
    application = create_celery_app(settings)
    queue_name = f"workflow-test-{uuid4().hex}"
    application.conf.task_queues = (Queue(queue_name),)
    application.conf.task_default_queue = queue_name
    application.conf.task_routes = {ADVANCE_WORKFLOW: {"queue": queue_name}}

    @asynccontextmanager
    async def runtime(
        config: Settings, factory: sessionmaker[Session]
    ) -> AsyncIterator[WorkflowEngine]:
        yield WorkflowEngine(WorkflowRecords(sessions, config), fixture.stages)

    def enqueue(self: CeleryWorkflowQueue, status: WorkflowStatus) -> None:
        application.send_task(
            ADVANCE_WORKFLOW,
            args=[str(status.id), status.stage, status.generation],
            queue=queue_name,
        )

    @contextmanager
    def resources(config: Settings) -> Iterator[tuple[WorkflowRecords, WorkflowQueue] | None]:
        yield fixture.records, CeleryWorkflowQueue(application)

    monkeypatch.setattr(workflow_resources, "workflow_runtime", runtime)
    monkeypatch.setattr(CeleryWorkflowQueue, "enqueue", enqueue)
    request = fixture.request()
    try:
        with start_worker(application, pool="solo", perform_ping_check=False, queues=[queue_name]):
            with TestClient(create_app(settings, workflow_factory=resources)) as client:
                response = client.post("/workflows", json=request.model_dump(mode="json"))
                assert response.status_code == 202
                assert client.post("/workflows", json=request.model_dump(mode="json")).json()[
                    "id"
                ] == str(request.request_id)
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    response = client.get(f"/workflows/{request.request_id}")
                    status = WorkflowStatus.model_validate(response.json())
                    if status.status in {"failed", "blocked", "completed"}:
                        break
                    time.sleep(0.2)
                assert status.status == "completed", status.model_dump()
                execution = client.get(f"/executions/{status.execution_id}")
                assert execution.status_code == 200 and execution.json()["status"] == "completed"
                assert "context" not in response.json()
                assert fixture.pushes == len(fixture.prs) == 1
                assert len(fixture.provider.calls) == 5
                from tests.integration.workflow_evidence import collect_evidence

                evidence = collect_evidence(sessions, status, tmp_path, transitions)
                evidence["sandbox"] = type(sandbox).__name__
                print("STEP24_EVIDENCE=" + json.dumps(evidence, sort_keys=True))
    finally:
        fixture.client.close()
        application.close()
