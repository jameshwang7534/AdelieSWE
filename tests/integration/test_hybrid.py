"""Hybrid HTTP search over PostgreSQL with deterministic semantic fixture vectors."""

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.session import session_scope
from app.indexing.embeddings import EmbeddingService
from app.indexing.scanner import RepositoryScanner
from app.indexing.service import IndexingService
from app.integrations.llm.embeddings import EmbeddingError
from app.main import create_app
from app.models import CodeChunk, Repository
from app.retrieval.base import Retriever
from app.retrieval.bm25 import BM25Retriever
from app.retrieval.postgres import PostgresChunkSource
from app.retrieval.vector import VectorRetriever
from tests.integration.test_database import engine as engine
from tests.integration.test_database import factory as factory
from tests.integration.test_indexing import setup_repository

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Requires PostgreSQL"
)


def test_hybrid_surfaces_symbol_and_concept(
    factory: sessionmaker[Session], tmp_path: Path, local_repository: Path
) -> None:
    identifier, workspace = setup_repository(factory, tmp_path / "workspace", local_repository)
    root = workspace.repository_path(identifier)
    (root / "a.py").write_text("def getUserById():\n    return None\n")
    (root / "concept.py").write_text("def resolve_identity():\n    return account_record\n")
    IndexingService(factory, workspace, RepositoryScanner()).index(identifier)
    query = "getUserById locate person"

    class Provider:
        dimensions = 1536
        profile = "hybrid-semantic-fixture"

        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            vectors = {
                "def getUserById():\n    return None\n": [0.0, 1.0, 0.0],
                "def resolve_identity():\n    return account_record\n": [1.0, 0.0, 0.0],
                "# Stable documentation\n": [0.0, 0.0, 1.0],
                query: [1.0, 0.0, 0.0],
                "getUserById": [0.0, 0.2, 0.8],
                "locate person": [1.0, 0.0, 0.0],
                "resolve_identity": [1.0, 0.0, 0.0],
            }
            return [vectors[text.replace("\r\n", "\n")] + [0.0] * 1533 for text in texts]

    provider = Provider()
    EmbeddingService(factory, workspace, provider).generate(identifier)
    bm25 = BM25Retriever(PostgresChunkSource(factory))
    vector = VectorRetriever(factory, provider)
    with session_scope(factory) as session:
        foreign = Repository(
            github_owner="foreign",
            github_name=str(uuid4()),
            clone_url="https://example.invalid/foreign",
            default_branch="main",
        )
        session.add(foreign)
        session.flush()
        private = CodeChunk(
            repository_id=foreign.id,
            file_path="foreign.py",
            language="python",
            start_line=1,
            end_line=1,
            content="getUserById resolve_identity",
            content_hash="foreign-hash",
            embedding=[1.0] + [0.0] * 1535,
            embedding_status="ready",
            embedding_profile=provider.profile,
            embedding_content_hash="foreign-hash",
        )
        session.add(private)
        session.flush()
        private_id = str(private.id)
    assert bm25.search(identifier, query)[0].file_path == "a.py"
    assert vector.search(identifier, query)[0].file_path == "concept.py"

    @contextmanager
    def lexical_resources(settings: Settings) -> Iterator[Retriever | None]:
        yield bm25

    @contextmanager
    def semantic_resources(settings: Settings) -> Iterator[VectorRetriever | None]:
        yield vector

    settings = Settings(database_url=None, redis_url=None, celery_broker_url=None, llm_api_key=None)
    application = create_app(
        settings, retrieval_factory=lexical_resources, vector_factory=semantic_resources
    )
    with TestClient(application) as api:
        url = f"/repositories/{identifier}/search"
        response = api.post(url, json={"query": query, "top_k": 2})
        assert response.status_code == 200
        hits = response.json()["results"]
        assert {row["file_path"] for row in hits} == {"a.py", "concept.py"}
        assert [row["rank"] for row in hits] == [1, 2]
        assert hits[0]["bm25_rank"] == 1
        assert hits[1]["bm25_rank"] is None and hits[1]["vector_rank"] == 1
        assert api.post(url, json={"query": query, "top_k": 2}).json() == response.json()
        assert api.post(url, json={"query": query, "top_k": 1}).json()["results"] == hits[:1]
        assert api.post(f"/repositories/{uuid4()}/search", json={"query": query}).status_code == 404
        assert api.post(url, json={"query": " "}).status_code == 422
        assert api.post(url, json={"query": query, "top_k": 101}).status_code == 422
        assert application.state.retriever.search(identifier, query, 2)[0].chunk_id.hex
        with patch.object(
            vector, "search", side_effect=EmbeddingError("sensitive-provider-detail")
        ):
            error = api.post(url, json={"query": query})
            assert error.status_code == 503 and "sensitive" not in error.text
        with patch.object(bm25, "search", side_effect=SQLAlchemyError("sensitive-database-detail")):
            error = api.post(url, json={"query": query})
            assert error.status_code == 503 and "sensitive" not in error.text
        print(f"Hybrid fixture: {[(hit['file_path'], hit['score']) for hit in hits]}")
        schema = api.get("/openapi.json").json()
        operation = schema["paths"]["/repositories/{repository_id}/search"]["post"]
        assert operation["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith("HybridSearchResponse")
        properties = schema["components"]["schemas"]["HybridSearchResult"]["properties"]
        expected = {
            "chunk_id",
            "file_path",
            "start_line",
            "end_line",
            "content",
            "snippet",
            "bm25_rank",
            "bm25_score",
            "vector_rank",
            "vector_score",
            "score",
            "rank",
        }
        assert set(properties) == expected
        for label, text, winner in [
            ("A exact symbol", "getUserById", "a.py"),
            ("B different wording", "locate person", "concept.py"),
            ("C agreement", "resolve_identity", "concept.py"),
        ]:
            lexical = bm25.search(identifier, text)
            semantic = vector.search(identifier, text)
            if label.startswith("A"):
                assert lexical[0].file_path == "a.py"
                assert semantic[0].file_path == "stable.md"
            elif label.startswith("B"):
                assert lexical == [] and semantic[0].file_path == "concept.py"
            else:
                assert lexical[0].chunk_id == semantic[0].chunk_id
            payload = {"query": text, "top_k": 3}
            result = api.post(url, json=payload)
            rows = result.json()["results"]
            assert result.status_code == 200 and rows[0]["file_path"] == winner
            assert len(rows) == len({row["chunk_id"] for row in rows})
            assert all(row["chunk_id"] != private_id for row in rows)
            assert api.post(url, json=payload).json() == result.json()
            assert api.post(url, json={**payload, "top_k": 1}).json()["results"] == rows[:1]
            for rank, row in enumerate(rows, 1):
                assert set(row) == expected and row["rank"] == rank
                assert row["snippet"] == row["content"]
                assert row["start_line"] == 1 and row["end_line"] in (1, 2)
                wanted = sum(
                    1 / (60 + row[key])
                    for key in ("bm25_rank", "vector_rank")
                    if row[key] is not None
                )
                assert row["score"] == pytest.approx(wanted)
            print(f"{label}: {[(row['file_path'], round(row['score'], 8)) for row in rows]}")
        # Existing embeddings from another model are not useful to this retriever.
        with patch.object(provider, "profile", "incompatible-model"):
            result = api.post(url, json={"query": "getUserById", "top_k": 3})
            rows = result.json()["results"]
            assert len(rows) == 1 and rows[0]["file_path"] == "a.py"
            assert rows[0]["vector_rank"] is None and rows[0]["vector_score"] is None
            assert rows[0]["score"] == pytest.approx(1 / 61)
            assert api.post(url, json={"query": "zzznomatch"}).json() == {"results": []}
            print(
                "D empty vector source: "
                f"{[(row['file_path'], round(row['score'], 8)) for row in rows]}"
            )
