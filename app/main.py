"""FastAPI factory with lifespan-owned dependency clients."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, asynccontextmanager

from fastapi import FastAPI

from app.api.status import router
from app.core.config import Settings
from app.services.readiness import Checks, readiness_resources

ResourceFactory = Callable[[Settings], AbstractContextManager[Checks]]


def create_app(
    settings: Settings | None = None,
    resource_factory: ResourceFactory = readiness_resources,
) -> FastAPI:
    """Build the API; network connections are first attempted by /ready."""
    configuration = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        logging.getLogger("app").setLevel(configuration.log_level)
        with resource_factory(configuration) as checks:
            application.state.checks = checks
            try:
                yield
            finally:
                del application.state.checks

    application = FastAPI(title=configuration.app_name, lifespan=lifespan)
    application.state.settings = configuration
    application.include_router(router)
    return application
