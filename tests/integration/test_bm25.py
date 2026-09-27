"""Repository isolation and fresh PostgreSQL corpus retrieval through the HTTP API."""

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.session import session_scope
from app.main import create_app
from app.models import CodeChunk, Repository
from app.retrieval.base import Retriever
from app.retrieval.bm25 import BM25Retriever
from app.retrieval.postgres import PostgresChunkSource
from tests.integration.test_database import engine as engine
from tests.integration.test_database import factory as factory

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Requires PostgreSQL"
)


def test_scoped_search_and_fresh_content(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        repositories = [
            Repository(
                github_owner="fixture",
                github_name=str(uuid4()),
                clone_url="https://example.invalid/repo.git",
                default_branch="main",
            )
            for _ in range(3)
        ]
        session.add_all(repositories)
        session.flush()
        target, other, empty = [repository.id for repository in repositories]
        chunks = []
        for repo_id, path, content in [
            (
                target,
                "src/accounts/users.py",
                "def get_user_by_id(user_id): return find_user(user_id)",
            ),
            (target, "orders.ts", "function getOrderById(orderId) { return findOrder(orderId); }"),
            (target, "geometry.py", "def area(radius): return radius * radius"),
            (other, "private.py", "getUserById getUserById getUserById privateSentinel"),
        ]:
            chunk = CodeChunk(
                repository_id=repo_id,
                file_path=path,
                language="python",
                start_line=4,
                end_line=4,
                content=content,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
            )
            session.add(chunk)
            chunks.append(chunk)
        session.flush()
        first_id = chunks[0].id
    retriever = BM25Retriever(PostgresChunkSource(factory))

    @contextmanager
    def resources(settings: Settings) -> Iterator[Retriever | None]:
        yield retriever

    settings = Settings(database_url=None, redis_url=None, celery_broker_url=None)
    with TestClient(create_app(settings, retrieval_factory=resources)) as api:
        url = f"/repositories/{target}/search/bm25"
        response = api.post(url, json={"query": "getUserById", "top_k": 2})
        assert response.status_code == 200
        results = response.json()["results"]
        assert [item["file_path"] for item in results] == ["src/accounts/users.py", "orders.ts"]
        assert results[0]["chunk_id"] == str(first_id)
        assert results[0]["start_line"] == results[0]["end_line"] == 4
        assert results[0]["content"].startswith("def get_user_by_id")
        assert results[0]["score"] > results[1]["score"] > 0
        assert [item["rank"] for item in results] == [1, 2]
        assert api.post(url, json={"query": "get_user_by_id", "top_k": 2}).json() == response.json()
        assert api.post(f"/repositories/{empty}/search/bm25", json={"query": "user"}).json() == {
            "results": []
        }
        assert (
            api.post(f"/repositories/{uuid4()}/search/bm25", json={"query": "user"}).status_code
            == 404
        )
        queries = [
            "get_user_by_id(user_id)",
            "user_id",
            "getUserById",
            "find user by id",
            "accounts",
        ]
        for query in queries:
            body = {"query": query, "top_k": 10}
            found = api.post(url, json=body)
            assert found.status_code == 200
            hits = found.json()["results"]
            assert hits and hits[0]["chunk_id"] == str(first_id)
            assert all(hit["file_path"] not in {"private.py", "geometry.py"} for hit in hits)
            assert [hit["rank"] for hit in hits] == list(range(1, len(hits) + 1))
            assert all(hit["score"] > 0 for hit in hits)
            for _ in range(3):
                assert api.post(url, json=body).json() == found.json()
            limited = api.post(url, json={"query": query, "top_k": 1}).json()["results"]
            assert limited == hits[:1]
            examples = [(hit["rank"], hit["file_path"], round(hit["score"], 6)) for hit in hits]
            print(f"BM25 audit query={query!r}: {examples}")
        for query in ["privateSentinel", "zzznomatchzzz", "---"]:
            assert api.post(url, json={"query": query}).json() == {"results": []}
        private = api.post(
            f"/repositories/{other}/search/bm25", json={"query": "getUserById"}
        ).json()["results"]
        assert len(private) == 1 and private[0]["file_path"] == "private.py"
        assert private[0]["chunk_id"] != str(first_id)
        with session_scope(factory) as session:
            session.execute(delete(CodeChunk).where(CodeChunk.id == first_id))
        assert api.post(url, json={"query": "user"}).json() == {"results": []}
