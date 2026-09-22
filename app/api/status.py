"""Liveness and dependency readiness; no exception text is exposed."""

import logging
from typing import Literal, cast

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from app.services.readiness import Checks, DependencyCheck

logger = logging.getLogger("app.readiness")
router = APIRouter(tags=["status"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class DependencyStatus(BaseModel):
    status: Literal["ok", "error", "unconfigured"]


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    dependencies: dict[str, DependencyStatus]


def probe(name: str, check: DependencyCheck | None) -> DependencyStatus:
    if check is None:
        return DependencyStatus(status="unconfigured")
    try:
        check.check()
    except Exception:
        # Report failures without exception messages, URLs, or credentials in logs.
        logger.warning("Readiness check failed: %s", name)
        return DependencyStatus(status="error")
    return DependencyStatus(status="ok")


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@router.get("/ready", response_model=ReadyResponse, responses={503: {"model": ReadyResponse}})
def ready(request: Request, response: Response) -> ReadyResponse:
    # Sync probes run in FastAPI's thread pool, not on the event loop.
    checks = cast(Checks, request.app.state.checks)
    dependencies = {
        "postgres": probe("postgres", checks.postgres),
        "redis": probe("redis", checks.redis),
    }
    is_ready = all(value.status == "ok" for value in dependencies.values())
    response.status_code = 200 if is_ready else 503
    return ReadyResponse(status="ready" if is_ready else "not_ready", dependencies=dependencies)
