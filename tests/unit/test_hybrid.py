"""Exact fusion arithmetic, deduplication, limits, and error propagation."""

from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.llm.embeddings import EmbeddingError
from app.main import create_app
from app.retrieval.hybrid import HybridRetrievalService
from app.schemas.search import SearchResult


def hit(path: str, rank: int, score: float = 1) -> SearchResult:
    return SearchResult(
        chunk_id=uuid4(),
        file_path=path,
        start_line=1,
        end_line=2,
        content="example code",
        score=score,
        rank=rank,
    )


def test_rrf_deduplicates_and_preserves_evidence() -> None:
    lexical, semantic, shared = (
        hit("lex.py", 1, 1000),
        hit("semantic.py", 1, 0.99),
        hit("both.py", 2),
    )
    bm25, vector = Mock(), Mock()
    bm25.search.return_value = [lexical, shared, lexical]
    vector.search.return_value = [semantic, shared.model_copy(update={"score": 0.7})]
    service = HybridRetrievalService(bm25, vector)
    repository_id = uuid4()
    results = service.search(repository_id, "query", 3)
    assert [row.file_path for row in results] == ["both.py", "lex.py", "semantic.py"]
    assert results[0].score == pytest.approx(2 / 62)
    assert results[1].score == results[2].score == pytest.approx(1 / 61)
    assert results[0].bm25_score == 1 and results[0].vector_score == 0.7
    assert results[1].vector_rank is None and results[2].bm25_rank is None
    assert [row.rank for row in results] == [1, 2, 3]
    assert all(row.snippet == row.content for row in results)
    assert service.search(repository_id, "query", 1) == results[:1]
    assert service.search(repository_id, "query", 3) == results
    bm25.search.assert_called_with(repository_id, "query", 50)
    vector.search.assert_called_with(repository_id, "query", 50)
    service.search(repository_id, "query", 100)
    vector.search.assert_called_with(repository_id, "query", 100)
    # Changing raw scales must not affect the fused scores or final ordering.
    bm25.search.return_value = [
        row.model_copy(update={"score": row.score * 1e9}) for row in [lexical, shared, lexical]
    ]
    vector.search.return_value = [
        row.model_copy(update={"score": 0.000001}) for row in [semantic, shared]
    ]
    rescaled = service.search(repository_id, "query", 3)
    assert [(row.chunk_id, row.score, row.rank) for row in rescaled] == [
        (row.chunk_id, row.score, row.rank) for row in results
    ]


def test_empty_sources_and_failure() -> None:
    bm25, vector = Mock(), Mock()
    bm25.search.return_value = []
    vector.search.return_value = []
    service = HybridRetrievalService(bm25, vector)
    assert service.search(uuid4(), "query") == []
    bm25.search.return_value = [hit("lexical.py", 1)]
    assert service.search(uuid4(), "query")[0].vector_rank is None
    bm25.search.return_value = []
    vector.search.return_value = [hit("semantic.py", 1)]
    assert len(service.search(uuid4(), "query")) == 1
    vector.search.side_effect = EmbeddingError("embedding_unavailable")
    with pytest.raises(EmbeddingError):
        service.search(uuid4(), "query")


def test_hybrid_configuration_optional_at_startup() -> None:
    settings = Settings(database_url=None, redis_url=None, celery_broker_url=None, llm_api_key=None)
    with TestClient(create_app(settings)) as api:
        assert api.get("/health").status_code == 200
        response = api.post(f"/repositories/{uuid4()}/search", json={"query": "hello"})
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "hybrid_search_unconfigured"
