"""Actual hybrid retrieval and bounded PostgreSQL issue-context reads through FastAPI."""

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.session import session_scope
from app.indexing.embeddings import EmbeddingService
from app.indexing.scanner import RepositoryScanner
from app.indexing.service import IndexingService
from app.integrations.llm.embeddings import EmbeddingError
from app.integrations.llm.fake_embeddings import FakeEmbeddingProvider
from app.main import create_app
from app.models import CodeChunk, Issue, Repository
from app.retrieval.base import Retriever
from app.retrieval.bm25 import BM25Retriever
from app.retrieval.hybrid import HybridRetrievalService
from app.retrieval.postgres import PostgresChunkSource
from app.retrieval.vector import VectorRetriever
from app.schemas.search import SearchResult
from app.services.context_store import ContextStore, PostgresContextStore
from tests.integration.test_database import engine as engine
from tests.integration.test_database import factory as factory
from tests.integration.test_indexing import setup_repository

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Requires PostgreSQL"
)


def test_issue_context_api(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path
) -> None:
    repo_id, workspace = setup_repository(factory, tmp_path / "workspace", local_repository)
    IndexingService(factory, workspace, RepositoryScanner()).index(repo_id)
    provider = FakeEmbeddingProvider()
    EmbeddingService(factory, workspace, provider).generate(repo_id)
    with session_scope(factory) as session:
        issue = Issue(
            repository_id=repo_id,
            github_issue_number=1,
            title="first",
            body="`first`\n" + "Details\n" * 5000,
            source_url="https://example.invalid/issue/1",
        )
        session.add(issue)
        foreign = Repository(
            github_owner="foreign",
            github_name=str(uuid4()),
            clone_url="https://example.invalid/repo",
            default_branch="main",
        )
        session.add(foreign)
        session.flush()
        issue_id = issue.id
        private = CodeChunk(
            repository_id=foreign.id,
            file_path="private.py",
            start_line=1,
            end_line=1,
            content="secret fixture",
            content_hash="test",
        )
        session.add(private)
        session.flush()
        private_id = private.id
        neighbor = CodeChunk(
            repository_id=repo_id,
            file_path="a.py",
            start_line=10,
            end_line=10,
            content="helper = 2\n",
            content_hash="neighbor",
        )
        session.add(neighbor)
        session.flush()
        neighbor_id = str(neighbor.id)
    store = PostgresContextStore(factory)
    assert store.chunks(repo_id, [private_id], 100) == []
    lexical = BM25Retriever(PostgresChunkSource(factory))
    vector = VectorRetriever(factory, provider)

    @contextmanager
    def context_resources(settings: Settings) -> Iterator[ContextStore | None]:
        yield store

    @contextmanager
    def lexical_resources(settings: Settings) -> Iterator[Retriever | None]:
        yield lexical

    @contextmanager
    def vector_resources(settings: Settings) -> Iterator[VectorRetriever | None]:
        yield vector

    settings = Settings(
        database_url=None,
        redis_url=None,
        celery_broker_url=None,
        llm_api_key=None,
        context_max_issue_chars=512,
        context_max_code_chars=100,
        context_max_queries=2,
        context_max_chunks=2,
        context_max_files=1,
    )
    app = create_app(
        settings,
        retrieval_factory=lexical_resources,
        vector_factory=vector_resources,
        context_factory=context_resources,
    )
    with TestClient(app) as api:
        assert isinstance(app.state.issue_context.retriever, HybridRetrievalService)
        url = f"/issues/{issue_id}/context"
        response = api.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data["repository"]["id"] == str(repo_id)
        assert data["issue"]["truncated"] and data["limited"]
        assert len(data["issue"]["title"] + data["issue"]["body"]) == 512
        assert len(data["queries"]) <= 2 and len(data["snippets"]) <= 2
        assert data["relevant_files"] == ["a.py"] and data["code_chars"] <= 100
        assert data["snippets"][0]["start_line"] == 1
        assert data["snippets"][0]["retrieval"][0]["bm25_score"] is not None
        assert data["snippets"][1]["chunk_id"] == neighbor_id
        assert data["snippets"][1]["neighbor_of"] == data["snippets"][0]["chunk_id"]
        assert data["snippets"][1]["start_line"] == 10
        assert data["snippets"][1]["retrieval"] == []
        assert api.get(url).json() == data
        assert api.get(f"/issues/{uuid4()}/context").status_code == 404
        with patch.object(provider, "embed", side_effect=EmbeddingError("sensitive")):
            error = api.get(url)
            assert error.status_code == 503 and "sensitive" not in error.text
        print(
            f"Issue context: issue_chars=512, code_chars={data['code_chars']}, "
            f"files={len(data['relevant_files'])}, snippets={len(data['snippets'])}, "
            f"queries={len(data['queries'])}"
        )
        # Even a faulty retriever cannot inject chunks from another repository.
        with patch.object(
            app.state.retriever,
            "search",
            return_value=[
                SearchResult(
                    chunk_id=private_id,
                    file_path="private.py",
                    start_line=1,
                    end_line=1,
                    content="secret fixture",
                    score=1,
                    rank=1,
                )
            ],
        ):
            scoped = api.get(url).json()
            assert scoped["snippets"] == [] and scoped["limited"]
            assert "secret fixture" not in json.dumps(scoped)
        with session_scope(factory) as session:
            record = session.get(Issue, issue_id)
            assert record is not None
            record.title = "first"
            record.body = None
        settings.context_max_chunks = 1
        example = api.get(url).json()
        assert len(example["snippets"]) == 1 and example["issue"]["body"] is None
        print("REPRESENTATIVE_CONTEXT=" + json.dumps(example))
        with session_scope(factory) as session:
            record = session.get(Issue, issue_id)
            assert record is not None
            record.title = "zzznomatch"
        with patch.object(provider, "profile", "incompatible"):
            with patch.object(
                provider, "embed", side_effect=AssertionError("No provider call needed")
            ):
                empty = api.get(url).json()
                assert empty["snippets"] == [] and empty["code_chars"] == 0
        for status in ["pending", "indexing", "error"]:
            with session_scope(factory) as session:
                repository = session.get(Repository, repo_id)
                assert repository is not None
                repository.index_status = status
            with patch.object(
                app.state.retriever, "search", side_effect=AssertionError("Index not ready")
            ):
                error = api.get(url)
                assert error.status_code == 409
                assert error.json()["detail"]["code"] == "repository_index_not_ready"
        with session_scope(factory) as session:
            repository = session.get(Repository, repo_id)
            assert repository is not None
            repository.index_status = "ready"
            session.execute(delete(CodeChunk).where(CodeChunk.repository_id == repo_id))
        assert api.get(url).json()["snippets"] == []
        with session_scope(factory) as session:
            session.execute(delete(Issue).where(Issue.id == issue_id))
            session.execute(delete(Repository).where(Repository.id == repo_id))
        assert api.get(url).status_code == 404
