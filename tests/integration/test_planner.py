"""Hybrid context -> fake LLM -> validated draft/tasks in real PostgreSQL."""

import json
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.session import session_scope
from app.indexing.embeddings import EmbeddingService
from app.indexing.scanner import RepositoryScanner
from app.indexing.service import IndexingService
from app.integrations.llm.fake_embeddings import FakeEmbeddingProvider
from app.integrations.llm.fake_provider import FakeLLMProvider
from app.integrations.llm.provider import LLMError, LLMProvider
from app.main import create_app
from app.models import (
    AgentRun,
    ExecutionRun,
    ImplementationPlan,
    Issue,
    PlanTask,
    Repository,
    TaskExecution,
)
from app.retrieval.base import Retriever
from app.retrieval.bm25 import BM25Retriever
from app.retrieval.hybrid import HybridRetrievalService
from app.retrieval.postgres import PostgresChunkSource
from app.retrieval.vector import VectorRetriever
from app.services.context_store import ContextStore, PostgresContextStore
from app.services.plans import PlanService
from tests.integration.test_database import engine as engine
from tests.integration.test_database import factory as factory
from tests.integration.test_indexing import setup_repository
from tests.unit.test_planning import proposal, task

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Requires PostgreSQL"
)


@pytest.mark.parametrize(
    "invalid",
    [
        "not json",
        json.dumps({"summary": "empty", "tasks": []}),
        json.dumps({"summary": "self", "tasks": [task("A", ("A",)).model_dump()]}),
        json.dumps({"summary": "missing", "tasks": [task("A", ("unknown",)).model_dump()]}),
        json.dumps({"summary": "duplicate", "tasks": [task("A").model_dump()] * 2}),
        json.dumps(
            {
                "summary": "cycle",
                "tasks": [task("A", ("B",)).model_dump(), task("B", ("A",)).model_dump()],
            }
        ),
    ],
    ids=["malformed", "empty", "self", "missing", "duplicate", "cycle"],
)
def test_planning_api(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path, invalid: str
) -> None:
    repo_id, workspace = setup_repository(factory, tmp_path / "workspace", local_repository)
    IndexingService(factory, workspace, RepositoryScanner()).index(repo_id)
    embeddings = FakeEmbeddingProvider()
    EmbeddingService(factory, workspace, embeddings).generate(repo_id)
    with session_scope(factory) as session:
        issue = Issue(
            repository_id=repo_id,
            github_issue_number=1,
            title="first",
            body="Improve `first`",
            source_url="https://example.invalid/issues/1",
        )
        session.add(issue)
        session.flush()
        issue_id = issue.id

    valid = proposal(task("B", ("A",)), task("A"))
    provider = FakeLLMProvider([invalid, valid.model_dump_json()])

    @asynccontextmanager
    async def llm_resources(settings: Settings) -> AsyncIterator[LLMProvider]:
        yield provider

    @contextmanager
    def context_resources(settings: Settings) -> Iterator[ContextStore | None]:
        yield PostgresContextStore(factory)

    @contextmanager
    def lexical_resources(settings: Settings) -> Iterator[Retriever | None]:
        yield BM25Retriever(PostgresChunkSource(factory))

    @contextmanager
    def vector_resources(settings: Settings) -> Iterator[VectorRetriever | None]:
        yield VectorRetriever(factory, embeddings)

    @contextmanager
    def plan_resources(settings: Settings) -> Iterator[PlanService | None]:
        yield PlanService(factory)

    settings = Settings(database_url=None, redis_url=None, celery_broker_url=None, llm_api_key=None)
    application = create_app(
        settings,
        retrieval_factory=lexical_resources,
        vector_factory=vector_resources,
        context_factory=context_resources,
        plan_factory=plan_resources,
        llm_factory=llm_resources,
    )
    with TestClient(application) as api:
        assert isinstance(application.state.issue_context.retriever, HybridRetrievalService)
        response = api.post(f"/issues/{issue_id}/plan")
        assert response.status_code == 201, response.text
        saved = response.json()
        assert saved["issue_id"] == str(issue_id) and saved["status"] == "draft"
        assert [item["task_key"] for item in saved["tasks"]] == ["A", "B"]
        assert [item["sequence"] for item in saved["tasks"]] == [0, 1]
        assert saved["tasks"][1]["dependencies"] == ["A"]
        assert all(item["status"] == "pending" for item in saved["tasks"])
        assert saved["tasks"][0]["rationale"] == valid.tasks[1].rationale
        assert saved["tasks"][0]["acceptance_criteria"] == list(valid.tasks[1].acceptance_criteria)
        assert saved["tasks"][0]["suggested_tests"] == list(valid.tasks[1].suggested_tests)
        assert len(provider.calls) == 2
        context = json.loads(provider.calls[0][1].content.split("\n", 1)[1])
        assert context["repository"]["id"] == str(repo_id)
        assert context["issue"]["id"] == str(issue_id) and context["snippets"]
        assert any(item["retrieval"] for item in context["snippets"])
        evidence = [entry for item in context["snippets"] for entry in item["retrieval"]]
        assert any(entry["bm25_rank"] is not None for entry in evidence)
        assert any(entry["vector_rank"] is not None for entry in evidence)
        with patch.object(
            provider, "generate", side_effect=AssertionError("GET must not call LLM")
        ):
            assert api.get(f"/plans/{saved['id']}").json() == saved
        assert api.get(f"/plans/{uuid4()}").status_code == 404
        assert api.post(f"/issues/{uuid4()}/plan").status_code == 404
        with patch.object(provider, "generate", side_effect=LLMError("llm_authentication_failed")):
            failure = api.post(f"/issues/{issue_id}/plan")
            assert failure.status_code == 503
            assert failure.json()["detail"]["code"] == "llm_authentication_failed"

        provider = FakeLLMProvider(invalid)
        failure = api.post(f"/issues/{issue_id}/plan")
        assert failure.status_code == 422
        assert failure.json()["detail"]["code"] == "planner_invalid_proposal"
        assert len(provider.calls) == 3
        with session_scope(factory) as session:
            assert session.scalar(select(func.count()).select_from(ImplementationPlan)) == 1
            assert session.scalar(select(func.count()).select_from(PlanTask)) == 2
            assert session.scalar(select(func.count()).select_from(ExecutionRun)) == 0
            assert session.scalar(select(func.count()).select_from(TaskExecution)) == 0
            assert session.scalar(select(func.count()).select_from(AgentRun)) == 0
            repository = session.get(Repository, repo_id)
            assert repository is not None
            repository.index_status = "pending"
        assert api.post(f"/issues/{issue_id}/plan").status_code == 409
        assert len(provider.calls) == 3
        assert PlanService(factory).get(UUID(saved["id"])).tasks[1].dependencies == ["A"]
