"""Lazy, stage-scoped production resource assembly; no work at module import."""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.integrations.git.runner import SubprocessGitRunner
from app.integrations.github.client import HttpGitHubClient
from app.integrations.llm.embeddings import EmbeddingProvider
from app.orchestration.workflow import WorkflowEngine
from app.orchestration.workflow_records import WorkflowError, WorkflowRecords
from app.orchestration.workflow_stages import WorkflowStages
from app.pull_requests.git import LocalPublicationGit
from app.sandbox.docker import DockerSandbox
from app.services.embedding_resources import embedding_resources
from app.services.llm_resources import llm_resources
from app.services.workspace import WorkspaceService


@asynccontextmanager
async def workflow_runtime(
    settings: Settings, sessions: sessionmaker[Session]
) -> AsyncIterator[WorkflowEngine]:
    @contextmanager
    def embeddings() -> Iterator[EmbeddingProvider]:
        with embedding_resources(settings) as provider:
            if provider is None:
                raise WorkflowError("workflow_embeddings_unconfigured")
            yield provider

    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token.get_secret_value()}"
    with httpx.Client(
        base_url=settings.github_api_url.rstrip("/") + "/",
        headers=headers,
        timeout=30,
        follow_redirects=False,
    ) as client:
        github = HttpGitHubClient(client)
        stages = WorkflowStages(
            settings,
            sessions,
            github,
            github,
            WorkspaceService(
                settings.workspace_root,
                SubprocessGitRunner(),
                github_host=settings.github_git_host,
                token=settings.github_token,
            ),
            lambda: llm_resources(settings),
            embeddings,
            DockerSandbox(settings),
            LocalPublicationGit(settings),
        )
        yield WorkflowEngine(WorkflowRecords(sessions, settings), stages)
