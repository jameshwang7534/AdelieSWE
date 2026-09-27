"""Context budgets and deterministic query/neighbor selection without external services."""

from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.retrieval.base import ChunkDocument
from app.schemas.context import ContextIssue, ContextRepository, ContextSeed
from app.schemas.search import HybridSearchResult
from app.services.context_queries import issue_queries
from app.services.issue_context import IssueContextService


def test_queries_are_deterministic_bounded_and_deduplicated() -> None:
    assert issue_queries("Fix user", "Use `getUser`\nUse `getUser`\nDetails", 3, 32) == [
        "Fix user",
        "getUser",
        "Use `getUser`",
    ]
    assert issue_queries("", None, 3, 32) == []
    queries = issue_queries("A" * 1000, "B" * 20000, 2, 100)
    assert len(queries) == 2 and all(len(query) <= 100 for query in queries)


def test_limits_deduplication_and_neighbors() -> None:
    repo, issue_id = uuid4(), uuid4()
    store, retrieval = Mock(), Mock()
    store.load_issue.return_value = ContextSeed(
        repository=ContextRepository(
            id=repo,
            github_owner="test",
            github_name="repo",
            default_branch="main",
            index_status="ready",
            embedding_status="ready",
        ),
        issue=ContextIssue(
            id=issue_id, github_issue_number=1, title="Fix user", body="getUser", truncated=False
        ),
    )
    first = ChunkDocument(uuid4(), "a.py", 2, 2, "x = 1\n")
    neighbor = ChunkDocument(uuid4(), "a.py", 1, 1, "import x\n")
    other = ChunkDocument(uuid4(), "b.py", 1, 1, "y = 2\n")
    big = ChunkDocument(uuid4(), "a.py", 3, 3, "z" * 1000)
    store.chunks.return_value = [first, other, big]
    store.neighbors.return_value = [neighbor, first]

    def result(doc: ChunkDocument, rank: int) -> HybridSearchResult:
        return HybridSearchResult(
            chunk_id=doc.id,
            file_path=doc.file_path,
            start_line=doc.start_line,
            end_line=doc.end_line,
            content=doc.content,
            snippet=doc.content,
            rank=rank,
            score=1 / rank,
            bm25_rank=rank,
            bm25_score=2,
        )

    retrieval.search.return_value = [
        result(first, 1),
        result(first, 1),
        result(other, 2),
        result(big, 3),
    ]
    settings = Settings(context_max_code_chars=20, context_max_files=1, context_max_chunks=4)
    service = IssueContextService(store, retrieval, settings)
    context = service.build(issue_id)
    assert [snippet.chunk_id for snippet in context.snippets] == [first.id, neighbor.id]
    assert context.relevant_files == ["a.py"] and context.code_chars == 15 and context.limited
    assert len(context.snippets[0].retrieval) == 2
    assert context.snippets[1].neighbor_of == first.id
    assert context.snippets[1].retrieval == []
    assert context == service.build(issue_id)
    settings.context_max_chunks = 1
    store.chunks.return_value = [first]
    store.neighbors.reset_mock()
    assert len(service.build(issue_id).snippets) == 1
    store.neighbors.assert_not_called()


def test_unconfigured_api_and_environment_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTEXT_MAX_CHUNKS", "2")
    settings = Settings(database_url=None, redis_url=None, celery_broker_url=None, llm_api_key=None)
    assert settings.context_max_chunks == 2
    with pytest.raises(ValueError):
        Settings(context_max_queries=100000)
    with TestClient(create_app(settings)) as api:
        assert api.get(f"/issues/{uuid4()}/context").status_code == 503
        assert api.get("/health").status_code == 200
