"""Safe GitHub reads and local persistence only; no GitHub write endpoints."""

import logging
from collections.abc import Iterator
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from sqlalchemy.exc import SQLAlchemyError

from app.integrations.github.client import GitHubError
from app.schemas.repositories import IssueResponse, RegisterRepository, RepositoryResponse
from app.services.repositories import RecordNotFound, RepositoryService

router = APIRouter(tags=["repositories and issues"])
logger = logging.getLogger("app.repositories")


def get_service(request: Request) -> Iterator[RepositoryService]:
    service = cast(RepositoryService | None, request.app.state.repository_service)
    if service is None:
        raise HTTPException(503, detail={"code": "database_unconfigured"})
    try:
        yield service
    except GitHubError as exc:
        logger.warning("GitHub operation failed: %s", exc.code)
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
        raise HTTPException(exc.status, detail={"code": exc.code}, headers=headers) from None
    except RecordNotFound:
        raise HTTPException(404, detail={"code": "record_not_found"}) from None
    except SQLAlchemyError:
        logger.warning("Repository database operation failed")
        raise HTTPException(503, detail={"code": "database_unavailable"}) from None


Service = Annotated[RepositoryService, Depends(get_service)]


@router.post("/repositories", response_model=RepositoryResponse)
def register_repository(body: RegisterRepository, service: Service) -> RepositoryResponse:
    return service.register(body.github_owner, body.github_name)


@router.get("/repositories/{repository_id}", response_model=RepositoryResponse)
def get_repository(repository_id: UUID, service: Service) -> RepositoryResponse:
    return service.get_repository(repository_id)


@router.post(
    "/repositories/{repository_id}/issues/{issue_number}/import", response_model=IssueResponse
)
def import_issue(
    repository_id: UUID,
    issue_number: Annotated[int, Path(gt=0)],
    service: Service,
) -> IssueResponse:
    return service.import_issue(repository_id, issue_number)


@router.get("/issues/{issue_id}", response_model=IssueResponse)
def get_issue(issue_id: UUID, service: Service) -> IssueResponse:
    return service.get_issue(issue_id)
