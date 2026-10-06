"""Real pgvector persistence/search and Redis worker tests with injected embeddings."""

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from celery.contrib.testing.worker import start_worker
from fastapi.testclient import TestClient
from kombu import Queue
from pydantic import SecretStr
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.session import create_session_factory, session_scope
from app.indexing.embeddings import EmbeddingService
from app.indexing.scanner import RepositoryScanner
from app.indexing.service import IndexingService
from app.integrations.llm.embeddings import EmbeddingError, EmbeddingProvider
from app.integrations.llm.fake_embeddings import FakeEmbeddingProvider
from app.main import create_app
from app.models import CodeChunk, Repository
from app.retrieval.vector import VectorRetriever
from app.workers.embeddings import EMBED_TASK_NAME
from app.workers.factory import create_celery_app
from tests.integration.test_database import engine as engine
from tests.integration.test_database import factory as factory
from tests.integration.test_indexing import setup_repository

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Requires PostgreSQL"
)


def test_semantic_fixture_ranking(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path
) -> None:
    """Known semantic vectors exercise ranking, without pretending to evaluate a real model."""
    identifier, workspace = setup_repository(factory, tmp_path / "workspaces", local_repository)
    root = workspace.repository_path(identifier)
    (root / "a.py").write_text("def verify_credentials():\n    return True\n")
    IndexingService(factory, workspace, RepositoryScanner()).index(identifier)

    class SemanticFixtureProvider:
        dimensions = 1536
        profile = "semantic-fixture-only"

        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            vectors = {
                "def verify_credentials():\n    return True\n": [1.0, 0.0],
                "# Stable documentation\n": [0.0, 1.0],
                "authenticate person": [0.8, 0.2],
            }
            return [vectors[text.replace("\r\n", "\n")] + [0.0] * 1534 for text in texts]

    provider = SemanticFixtureProvider()
    EmbeddingService(factory, workspace, provider).generate(identifier)
    retriever = VectorRetriever(factory, provider)
    hits = retriever.search(identifier, "authenticate person", 2)
    assert [hit.file_path for hit in hits] == ["a.py", "stable.md"]
    assert hits[0].distance == pytest.approx(0.0298575, abs=1e-6)
    assert hits[1].distance == pytest.approx(0.7574644, abs=1e-6)
    assert retriever.search(identifier, "authenticate person", 2) == hits
    print(f"Semantic fixture: {[(hit.file_path, hit.distance) for hit in hits]}")


def test_resume_reuse_and_reindex(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path
) -> None:
    identifier, workspace = setup_repository(factory, tmp_path / "workspaces", local_repository)
    indexer = IndexingService(factory, workspace, RepositoryScanner())
    indexer.index(identifier)
    provider = FakeEmbeddingProvider()
    service = EmbeddingService(factory, workspace, provider, batch_size=1)
    original = provider.embed
    calls = 0

    def failing(texts: list[str]) -> list[list[float]]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise EmbeddingError("synthetic-sensitive-error")
        return original(texts)

    with patch.object(provider, "embed", side_effect=failing):
        with pytest.raises(EmbeddingError, match="^embedding_generation_failed$"):
            service.generate(identifier)
    with session_scope(factory) as session:
        states = list(
            session.scalars(
                select(CodeChunk.embedding_status).where(CodeChunk.repository_id == identifier)
            )
        )
        assert sorted(states) == ["error", "ready"]
        repo = session.get(Repository, identifier)
        assert repo is not None and repo.embedding_status == "error"
    with patch.object(provider, "embed", wraps=original) as embed:
        assert service.generate(identifier)["embedded"] == 1
        assert service.generate(identifier)["embedded"] == 0
        assert embed.call_count == 1
        indexer.index(identifier)
        assert service.generate(identifier)["embedded"] == 0
        root = workspace.repository_path(identifier)
        (root / "a.py").write_text("def changed():\n    return 12\n")
        (root / "stable.md").unlink()
        indexer.index(identifier)
        assert service.generate(identifier)["embedded"] == 1
        assert embed.call_count == 2
    with session_scope(factory) as session:
        chunks = list(
            session.scalars(select(CodeChunk).where(CodeChunk.repository_id == identifier))
        )
        assert len(chunks) == 1 and chunks[0].embedding_status == "ready"
        assert chunks[0].embedding_content_hash == chunks[0].content_hash
        assert chunks[0].embedding is not None and len(chunks[0].embedding) == 1536
    provider.profile = "new-model-profile"
    assert service.generate(identifier)["embedded"] == 1
    provider.profile = "bad-provider-profile"
    with patch.object(provider, "embed", return_value=[[1.0, 0.0]]):
        with pytest.raises(EmbeddingError):
            service.generate(identifier)
    with session_scope(factory) as session:
        chunk = session.get(CodeChunk, chunks[0].id)
        assert chunk is not None and chunk.embedding_status == "error"
        assert chunk.embedding_profile == "new-model-profile"
    assert service.generate(identifier)["embedded"] == 1


def test_vector_api_and_isolation(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path
) -> None:
    identifier, workspace = setup_repository(factory, tmp_path / "workspaces", local_repository)
    IndexingService(factory, workspace, RepositoryScanner()).index(identifier)
    provider = FakeEmbeddingProvider()
    EmbeddingService(factory, workspace, provider).generate(identifier)
    retriever = VectorRetriever(factory, provider)

    @contextmanager
    def resources(settings: Settings) -> Iterator[VectorRetriever | None]:
        yield retriever

    with session_scope(factory) as session:
        other = Repository(
            github_owner="other",
            github_name=str(uuid4()),
            clone_url="https://example.invalid/r.git",
            default_branch="main",
        )
        session.add(other)
        session.flush()
        other_id = other.id
        session.add(
            CodeChunk(
                repository_id=other_id,
                file_path="private.py",
                language="python",
                start_line=1,
                end_line=1,
                content="def first():\n    return 1\n",
                content_hash="private",
                embedding=provider.embed(["def first():\n    return 1\n"])[0],
                embedding_status="ready",
                embedding_profile=provider.profile,
                embedding_content_hash="private",
            )
        )
    settings = Settings(database_url=None, redis_url=None, celery_broker_url=None, llm_api_key=None)
    with TestClient(create_app(settings, vector_factory=resources)) as api:
        url = f"/repositories/{identifier}/search/vector"
        body = {"query": "def first():\n    return 1\n", "top_k": 2}
        response = api.post(url, json=body)
        assert response.status_code == 200
        hits = response.json()["results"]
        assert len(hits) == 2 and hits[0]["file_path"] == "a.py"
        assert hits[0]["distance"] == pytest.approx(0, abs=1e-6)
        assert hits[0]["score"] == pytest.approx(1, abs=1e-6)
        assert hits[0]["distance"] < hits[1]["distance"]
        assert [hit["rank"] for hit in hits] == [1, 2]
        assert all(hit["file_path"] != "private.py" for hit in hits)
        assert api.post(url, json=body).json() == response.json()
        assert api.post(url, json={**body, "top_k": 1}).json()["results"] == hits[:1]
        assert api.post(url, json={"query": " "}).status_code == 422
        assert api.post(f"/repositories/{uuid4()}/search/vector", json=body).status_code == 404
        with patch.object(provider, "embed", side_effect=EmbeddingError("embedding_unavailable")):
            assert api.post(url, json=body).status_code == 503
        provider.profile = "different-model"
        with patch.object(
            provider, "embed", side_effect=AssertionError("Must skip incompatible vectors")
        ):
            assert api.post(url, json=body).json() == {"results": []}
        print(
            f"Vector search: {[(h['file_path'], h['distance'], h['rank']) for h in hits]}; "
            "cross-repository leaks=0"
        )


@pytest.mark.skipif(os.environ.get("RUN_WORKER_TESTS") != "1", reason="Requires Redis")
def test_embedding_worker(engine: Engine, tmp_path: Path, local_repository: Path) -> None:
    sessions = create_session_factory(engine)
    identifier, workspace = setup_repository(sessions, tmp_path / "workspaces", local_repository)
    IndexingService(sessions, workspace, RepositoryScanner()).index(identifier)
    settings = Settings(
        database_url=SecretStr(engine.url.render_as_string(hide_password=False)),
        workspace_root=workspace.root,
    )

    @contextmanager
    def resources(settings: Settings) -> Iterator[EmbeddingProvider | None]:
        yield FakeEmbeddingProvider()

    @contextmanager
    def unconfigured(settings: Settings) -> Iterator[EmbeddingProvider | None]:
        yield None

    app = create_celery_app(settings)
    queue = f"embedding-test-{uuid4().hex}"
    app.conf.task_queues = (Queue(queue),)
    app.conf.task_default_queue = queue
    app.conf.task_routes = {EMBED_TASK_NAME: {"queue": queue}}
    tasks: list[str] = []
    try:
        with (
            patch("app.workers.deliveries.Settings", return_value=settings),
            patch("app.workers.embeddings.Settings", return_value=settings),
            patch("app.services.embedding_resources.embedding_resources", resources),
            start_worker(app, pool="solo", perform_ping_check=False, queues=[queue]),
        ):
            for count in [2, 0]:
                task = app.send_task(EMBED_TASK_NAME, args=[str(identifier)])
                tasks.append(str(task.id))
                assert task.get(timeout=45)["embedded"] == count
                assert task.state == "SUCCESS"
            print(f"Embedding worker: tasks={tasks}; embedded counts=[2, 0]")
            with patch("app.services.embedding_resources.embedding_resources", unconfigured):
                task = app.send_task(EMBED_TASK_NAME, args=[str(identifier)])
                tasks.append(str(task.id))
                failure = task.get(timeout=45, propagate=False)
                assert task.state == "FAILURE"
                assert "embedding_unconfigured: set LLM_API_KEY" in str(failure)
    finally:
        try:
            for task_id in tasks:
                app.backend.forget(task_id)
            with app.connection_for_write() as connection:
                Queue(queue)(connection.default_channel).delete(if_unused=True, if_empty=True)
        finally:
            app.backend.result_consumer.stop()
            app.backend.client.close()
            app.close()
            with session_scope(sessions) as session:
                session.execute(delete(CodeChunk).where(CodeChunk.repository_id == identifier))
                session.execute(delete(Repository).where(Repository.id == identifier))
