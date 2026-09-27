"""Deterministic lexical relevance and API behavior without a database."""

import math
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.main import create_app
from app.retrieval.base import ChunkDocument, Retriever
from app.retrieval.bm25 import BM25Retriever
from app.retrieval.tokenization import tokenize


class MemorySource:
    def __init__(self, documents: Sequence[ChunkDocument]) -> None:
        self.documents = documents

    def load(self, repository_id: UUID) -> Sequence[ChunkDocument]:
        return self.documents


def document(identifier: int, path: str, content: str) -> ChunkDocument:
    return ChunkDocument(UUID(int=identifier), path, 1, 3, content)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("get_user_by_id", ["get", "user", "by", "id"]),
        ("getUserById", ["get", "user", "by", "id"]),
        ("HTTPClient", ["http", "client"]),
        ("src/api/http_client.ts", ["src", "api", "http", "client", "ts"]),
        ("Find a USER by ID!", ["find", "a", "user", "by", "id"]),
        ("parseHTTP2Response", ["parse", "http", "2", "response"]),
    ],
)
def test_tokenization(text: str, expected: list[str]) -> None:
    assert tokenize(text) == expected


@pytest.mark.parametrize("query", ["get_user_by_id", "getUserById", "get user by id"])
def test_relevant_code_ranks_first(query: str) -> None:
    source = MemorySource(
        [
            document(1, "src/users.py", "def get_user_by_id(user_id): return users.find(user_id)"),
            document(
                2, "src/orders.py", "def get_order_by_id(order_id): return orders.find(order_id)"
            ),
            document(3, "src/render.py", "def draw_circle(radius): return radius * radius"),
        ]
    )
    retriever = BM25Retriever(source)
    results = retriever.search(uuid4(), query, 10)
    assert [result.chunk_id.int for result in results] == [1, 2]
    assert results[0].score > results[1].score > 0
    assert [result.rank for result in results] == [1, 2]
    assert retriever.search(uuid4(), query, 1) == results[:1]
    assert retriever.search(uuid4(), query, 10) == results


def test_numeric_bm25_formula_and_repeated_query_terms() -> None:
    retriever = BM25Retriever(
        MemorySource(
            [
                document(1, "a", "term term"),
                document(2, "b", "other"),
            ]
        )
    )
    result = retriever.search(uuid4(), "term")[0]
    expected = math.log(2) * (2 * 2.5) / (2 + 1.5 * (0.25 + 0.75 * 3 / 2.5))
    assert result.score == pytest.approx(expected)
    assert retriever.search(uuid4(), "term term")[0].score == result.score


def test_paths_ties_common_terms_and_no_matches() -> None:
    docs = [
        document(2, "src/http_client.ts", "return value"),
        document(1, "src/http_client.ts", "return value"),
    ]
    retriever = BM25Retriever(MemorySource(docs))
    assert [r.chunk_id.int for r in retriever.search(uuid4(), "HTTPClient")] == [1, 2]
    assert all(r.score > 0 for r in retriever.search(uuid4(), "return"))
    assert retriever.search(uuid4(), "nonexistent") == []
    assert retriever.search(uuid4(), "---") == []
    assert BM25Retriever(MemorySource([])).search(uuid4(), "anything") == []


def test_api_validation_failure_redaction_and_cleanup(caplog: pytest.LogCaptureFixture) -> None:
    retriever = Mock(spec=Retriever)
    retriever.search.return_value = []
    closed: list[bool] = []

    @contextmanager
    def resources(settings: Settings) -> Iterator[Retriever | None]:
        try:
            yield retriever
        finally:
            closed.append(True)

    settings = Settings(database_url=None, redis_url=None, celery_broker_url=None)
    with TestClient(create_app(settings, retrieval_factory=resources)) as api:
        identifier = uuid4()
        url = f"/repositories/{identifier}/search/bm25"
        assert api.post(url, json={"query": "user", "top_k": 3}).json() == {"results": []}
        retriever.search.assert_called_once_with(identifier, "user", 3)
        for body in [
            {"query": ""},
            {"query": "   "},
            {"query": "x" * 2001},
            {"query": "user", "top_k": 0},
            {"query": "user", "top_k": 101},
        ]:
            assert api.post(url, json=body).status_code == 422
        assert (
            api.post("/repositories/invalid/search/bm25", json={"query": "user"}).status_code == 422
        )
        retriever.search.side_effect = OperationalError("synthetic-secret", {}, Exception())
        response = api.post(url, json={"query": "user"})
        assert response.status_code == 503
        assert "synthetic-secret" not in response.text + caplog.text
        assert api.get("/health").status_code == 200
    assert closed == [True]
    with TestClient(create_app(settings)) as api:
        assert api.post(url, json={"query": "user"}).status_code == 503
