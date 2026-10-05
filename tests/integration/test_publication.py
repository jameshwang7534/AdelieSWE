"""Real Git and PostgreSQL with mocked GitHub HTTP and push; never publishes externally."""

import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.coding import CodingAgent
from app.agents.review import ReviewAgent
from app.core.config import Settings
from app.db.session import session_scope
from app.integrations.git.patches import LocalPatchGit
from app.integrations.github.client import HttpGitHubClient
from app.integrations.llm.fake_provider import FakeLLMProvider
from app.models import AgentRun, PullRequest, Repository, TaskExecution
from app.pull_requests.contracts import Publication, PublicationError, PublishedPR
from app.pull_requests.git import LocalPublicationGit
from app.pull_requests.records import PublicationRecords
from app.pull_requests.service import PullRequestService
from app.sandbox.base import SandboxResult
from app.schemas.testing import RepositoryTestConfig
from app.services.code_patches import CodePatchService
from app.services.coding import CodingService
from app.services.coding_records import CodingRecords
from app.services.executions import ExecutionService
from app.services.review import ReviewService
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "success",
        "blocking",
        "failed-tests",
        "incomplete",
        "changed",
        "empty",
        "remote-collision",
        "push-failure",
        "push-response-lost",
        "api-response-lost",
        "database-failure",
        "closed-pr",
        "pr-collision",
        "secret-diff",
        "disabled-tests",
    ],
)
async def test_publication(
    factory: sessionmaker[Session],
    tmp_path: Path,
    local_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    run_id, _, context, workspace = setup_run(factory, tmp_path, local_repository)
    settings = Settings(workspace_root=tmp_path)
    required = RepositoryTestConfig(required_commands=("python -m unittest",))
    with session_scope(factory) as session:
        repository = session.get(Repository, context.repository.id)
        assert repository is not None
        repository.clone_url = (
            f"https://github.com/{repository.github_owner}/{repository.github_name}.git"
        )
    before = "original"
    for task in ExecutionService(factory).get(run_id).tasks:
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
                    stdout="Passed",
                    stderr="",
                    elapsed_seconds=0.1,
                    timed_out=False,
                    output_truncated=False,
                )
            ]
        )
        ValidationService(settings, sandbox, Records(factory)).run(run_id, task.id, required)
        before = task.task_key
    await ReviewService(
        settings,
        ReviewAgent(
            FakeLLMProvider(
                json.dumps(
                    {
                        "summary": "Implemented issue",
                        "approved": True,
                        "findings": [],
                    }
                )
            )
        ),
        ReviewRecords(factory),
        CodePatchService(tmp_path),
    ).run(run_id, context, required)
    if case == "secret-diff":
        # Configure a synthetic credential matching already-reviewed text: block before commit.
        from pydantic import SecretStr

        settings.github_token = SecretStr("+D")
    if case == "disabled-tests":
        settings.test_allowed_commands = ()
    with session_scope(factory) as session:
        if case == "blocking":
            review = session.scalar(select(AgentRun).where(AgentRun.agent_type == "review"))
            assert review is not None
            data = json.loads(json.dumps(review.output_metadata))
            data["decision"]["review"]["findings"] = [
                {"severity": "blocking", "description": "Bug", "recommendation": "Fix"}
            ]
            review.output_metadata = data
        if case == "failed-tests":
            test = session.scalar(select(AgentRun).where(AgentRun.agent_type == "test"))
            assert test is not None
            data = json.loads(json.dumps(test.output_metadata))
            data["report"]["results"][0]["exit_code"] = 1
            test.output_metadata = data
        if case == "incomplete":
            task_record = session.scalar(select(TaskExecution))
            assert task_record is not None
            task_record.status = "running"
    if case in {"changed", "empty"}:
        (workspace / "hello.txt").write_bytes(b"original\n" if case == "empty" else b"other\n")
    head = LocalPatchGit().run(["rev-parse", "HEAD"], workspace).stdout
    remote_sha: str | None = "0" * 40 if case == "remote-collision" else None
    prs: list[dict[str, Any]] = []
    pushes = 0
    creates = 0
    requests: list[httpx.Request] = []

    class MockPush(LocalPublicationGit):
        def push(self, workspace: Path, publication: Publication) -> None:
            nonlocal remote_sha, pushes
            pushes += 1
            if case == "push-failure" and pushes == 1:
                raise PublicationError("push_failed")
            remote_sha = publication.commit
            if case == "push-response-lost" and pushes == 1:
                raise PublicationError("push_response_lost")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal creates
        requests.append(request)
        if "/git/ref/" in request.url.path:
            return httpx.Response(
                404 if remote_sha is None else 200, json={"object": {"sha": remote_sha}}
            )
        if request.method == "GET":
            assert request.url.params["state"] == "all"
            return httpx.Response(200, json=prs)
        creates += 1
        payload = json.loads(request.content)
        assert (
            payload["title"]
            == (f"Resolve #{context.issue.github_issue_number}: {context.issue.title}"[:250])
        )
        assert (
            payload["head"] == f"ai-platform/issue-{context.issue.github_issue_number}-{run_id.hex}"
        )
        assert payload["base"] == context.repository.default_branch
        repo = f"{context.repository.github_owner}/{context.repository.github_name}"
        data = {
            "number": 7,
            "html_url": f"https://github.com/{repo}/pull/7",
            "state": "open",
            "body": payload["body"],
            "head": {"ref": payload["head"], "sha": remote_sha, "repo": {"full_name": repo}},
            "base": {"ref": payload["base"], "repo": {"full_name": repo}},
        }
        prs.append(data)
        if case == "api-response-lost":
            raise httpx.ReadTimeout("simulated lost response", request=request)
        return httpx.Response(201, json=data)

    records = PublicationRecords(factory)
    finish = records.finish

    def fail_finish(publication: Publication, journal_id: UUID, remote: PublishedPR) -> PublishedPR:
        raise RuntimeError("simulated persistence failure")

    if case == "database-failure":
        monkeypatch.setattr(records, "finish", fail_finish)
    with httpx.Client(
        base_url="https://api.github.com/", transport=httpx.MockTransport(handler)
    ) as client:
        service = PullRequestService(
            settings, records, MockPush(settings), HttpGitHubClient(client)
        )
        if case in {
            "blocking",
            "failed-tests",
            "incomplete",
            "changed",
            "empty",
            "remote-collision",
            "secret-diff",
            "disabled-tests",
        }:
            with pytest.raises(PublicationError):
                service.run(run_id, context, required)
            assert creates == pushes == 0
            if case != "remote-collision":
                assert requests == []
            if case == "secret-diff":
                refs = LocalPatchGit().run(["for-each-ref", "refs/heads/ai-platform/"], workspace)
                assert refs.stdout == ""
            return
        if case in {"push-failure", "push-response-lost", "api-response-lost", "database-failure"}:
            with pytest.raises(PublicationError):
                service.run(run_id, context, required)
            monkeypatch.setattr(records, "finish", finish)
        first = service.run(run_id, context, required)
        assert first.number == 7
        if case == "closed-pr":
            prs[0]["state"] = "closed"
        if case == "pr-collision":
            prs[0]["body"] = "Unrelated PR"
            with pytest.raises(PublicationError, match="publication_pr_collision"):
                service.run(run_id, context, required)
        else:
            assert service.run(run_id, context, required).state == (
                "closed" if case == "closed-pr" else "open"
            )
    assert creates == 1
    assert pushes == (2 if case == "push-failure" else 1)
    assert LocalPatchGit().run(["rev-parse", "HEAD"], workspace).stdout == head
    body = prs[0]["body"]
    if case != "pr-collision":
        for value in (
            str(run_id),
            "Implementation summary",
            "Implementation plan",
            "hello.txt",
            "python -m unittest discover",
            "PASS",
            "Review summary",
            "Known limitations",
            f"Issue: {context.repository.github_owner}/{context.repository.github_name}"
            f"#{context.issue.github_issue_number}",
            "Implemented issue",
            "No blocking findings.",
        ):
            assert value in body
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(PullRequest)) == 1
        record = session.scalar(select(PullRequest))
        assert record is not None and record.execution_run_id == run_id
        assert record.github_pr_number == 7 and record.github_url == prs[0]["html_url"]
        assert record.status == ("closed" if case == "closed-pr" else "open")
        assert (
            session.scalar(
                select(func.count())
                .select_from(AgentRun)
                .where(AgentRun.agent_type == "pull_request")
            )
            == 1
        )
