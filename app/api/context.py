"""Read-only issue context endpoint; no planning or GitHub operations."""

import logging
from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from app.integrations.llm.embeddings import EmbeddingError
from app.retrieval.base import RepositoryNotFound
from app.schemas.context import IssueContext
from app.services.issue_context import IssueContextNotReady, IssueContextService
from app.services.repositories import RecordNotFound

router = APIRouter(tags=["issue context"])
logger = logging.getLogger("app.context")


@router.get("/issues/{issue_id}/context", response_model=IssueContext)
def issue_context(issue_id: UUID, request: Request) -> IssueContext:
    service = cast(IssueContextService | None, request.app.state.issue_context)
    if service is None:
        raise HTTPException(503, detail={"code": "issue_context_unconfigured"})
    try:
        return service.build(issue_id)
    except (RecordNotFound, RepositoryNotFound):
        raise HTTPException(404, detail={"code": "record_not_found"}) from None
    except IssueContextNotReady:
        raise HTTPException(409, detail={"code": "repository_index_not_ready"}) from None
    except EmbeddingError:
        logger.warning("Issue context embedding failed")
        raise HTTPException(503, detail={"code": "embedding_unavailable"}) from None
    except SQLAlchemyError:
        logger.warning("Issue context database operation failed")
        raise HTTPException(503, detail={"code": "database_unavailable"}) from None
