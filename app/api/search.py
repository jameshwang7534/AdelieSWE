"""Repository-scoped lexical and vector search endpoints."""

import logging
from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from app.integrations.llm.embeddings import EmbeddingError
from app.retrieval.base import RepositoryNotFound, Retriever
from app.retrieval.hybrid import HybridRetrievalService
from app.retrieval.vector import VectorRetriever
from app.schemas.search import (
    HybridSearchResponse,
    SearchRequest,
    SearchResponse,
    VectorSearchResponse,
)

router = APIRouter(tags=["search"])
logger = logging.getLogger("app.search")


@router.post("/repositories/{repository_id}/search", response_model=HybridSearchResponse)
def search_hybrid(
    repository_id: UUID, body: SearchRequest, request: Request
) -> HybridSearchResponse:
    retriever = cast(HybridRetrievalService | None, request.app.state.retriever)
    if retriever is None:
        raise HTTPException(503, detail={"code": "hybrid_search_unconfigured"})
    try:
        return HybridSearchResponse(results=retriever.search(repository_id, body.query, body.top_k))
    except RepositoryNotFound:
        raise HTTPException(404, detail={"code": "repository_not_found"}) from None
    except EmbeddingError:
        logger.warning("Hybrid query embedding failed")
        raise HTTPException(503, detail={"code": "embedding_unavailable"}) from None
    except SQLAlchemyError:
        logger.warning("Hybrid search database operation failed")
        raise HTTPException(503, detail={"code": "database_unavailable"}) from None


@router.post("/repositories/{repository_id}/search/bm25", response_model=SearchResponse)
def search_bm25(repository_id: UUID, body: SearchRequest, request: Request) -> SearchResponse:
    retriever = cast(Retriever | None, request.app.state.bm25_retriever)
    if retriever is None:
        raise HTTPException(503, detail={"code": "database_unconfigured"})
    try:
        return SearchResponse(results=retriever.search(repository_id, body.query, body.top_k))
    except RepositoryNotFound:
        raise HTTPException(404, detail={"code": "repository_not_found"}) from None
    except SQLAlchemyError:
        logger.warning("BM25 database operation failed")
        raise HTTPException(503, detail={"code": "database_unavailable"}) from None


@router.post("/repositories/{repository_id}/search/vector", response_model=VectorSearchResponse)
def search_vector(
    repository_id: UUID, body: SearchRequest, request: Request
) -> VectorSearchResponse:
    retriever = cast(VectorRetriever | None, request.app.state.vector_retriever)
    if retriever is None:
        raise HTTPException(503, detail={"code": "vector_search_unconfigured"})
    try:
        return VectorSearchResponse(results=retriever.search(repository_id, body.query, body.top_k))
    except RepositoryNotFound:
        raise HTTPException(404, detail={"code": "repository_not_found"}) from None
    except EmbeddingError:
        logger.warning("Query embedding failed")
        raise HTTPException(503, detail={"code": "embedding_unavailable"}) from None
    except SQLAlchemyError:
        logger.warning("Vector search database operation failed")
        raise HTTPException(503, detail={"code": "database_unavailable"}) from None
