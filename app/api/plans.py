"""Generate draft plans and read saved plans; never execute them."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.agents.planner import PlannerAgent, PlannerValidationError
from app.core.config import Settings
from app.integrations.llm.embeddings import EmbeddingError
from app.integrations.llm.provider import LLMError, LLMProvider
from app.retrieval.base import RepositoryNotFound
from app.schemas.plans import PlanResponse
from app.services.issue_context import IssueContextNotReady, IssueContextService
from app.services.plans import PlanService
from app.services.repositories import RecordNotFound

LLMFactory = Callable[[Settings], AbstractAsyncContextManager[LLMProvider]]
router = APIRouter(tags=["plans"])


@router.post("/issues/{issue_id}/plan", response_model=PlanResponse, status_code=201)
async def create_plan(issue_id: UUID, request: Request) -> PlanResponse:
    contexts = cast(IssueContextService | None, request.app.state.issue_context)
    plans = cast(PlanService | None, request.app.state.plan_service)
    if contexts is None or plans is None:
        raise HTTPException(503, detail={"code": "planner_unconfigured"})
    factory = cast(LLMFactory, request.app.state.llm_factory)
    settings = cast(Settings, request.app.state.settings)
    try:
        async with factory(settings) as provider:
            context = await run_in_threadpool(contexts.build, issue_id)
            proposal = await PlannerAgent(provider, settings.planner_validation_retries).propose(
                context
            )
        identifier = await run_in_threadpool(plans.create, issue_id, proposal)
        return await run_in_threadpool(plans.get, identifier)
    except (RecordNotFound, RepositoryNotFound):
        raise HTTPException(404, detail={"code": "record_not_found"}) from None
    except IssueContextNotReady:
        raise HTTPException(409, detail={"code": "repository_index_not_ready"}) from None
    except PlannerValidationError:
        raise HTTPException(422, detail={"code": "planner_invalid_proposal"}) from None
    except LLMError as error:
        raise HTTPException(503, detail={"code": error.code}) from None
    except EmbeddingError:
        raise HTTPException(503, detail={"code": "embedding_unavailable"}) from None
    except SQLAlchemyError:
        raise HTTPException(503, detail={"code": "database_unavailable"}) from None


@router.get("/plans/{plan_id}", response_model=PlanResponse)
def get_plan(plan_id: UUID, request: Request) -> PlanResponse:
    plans = cast(PlanService | None, request.app.state.plan_service)
    if plans is None:
        raise HTTPException(503, detail={"code": "database_unconfigured"})
    try:
        return plans.get(plan_id)
    except RecordNotFound:
        raise HTTPException(404, detail={"code": "record_not_found"}) from None
    except SQLAlchemyError:
        raise HTTPException(503, detail={"code": "database_unavailable"}) from None
