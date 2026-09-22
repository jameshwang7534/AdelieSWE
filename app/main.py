"""FastAPI factory with lifespan-owned dependency clients."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, asynccontextmanager

from fastapi import FastAPI

from app.api.repositories import router as repositories_router
from app.api.status import router
from app.api.tasks import router as tasks_router
from app.core.config import Settings
from app.services.github_resources import repository_resources
from app.services.readiness import Checks, readiness_resources
from app.services.repositories import RepositoryService
from app.services.task_queue import TaskQueue, task_queue_resources

ResourceFactory = Callable[[Settings], AbstractContextManager[Checks]]
QueueFactory = Callable[[Settings], AbstractContextManager[TaskQueue | None]]
RepositoryFactory = Callable[[Settings], AbstractContextManager[RepositoryService | None]]


def create_app(
    settings: Settings | None = None,
    resource_factory: ResourceFactory = readiness_resources,
    queue_factory: QueueFactory = task_queue_resources,
    repository_factory: RepositoryFactory = repository_resources,
) -> FastAPI:
    """Build the API; connections are opened on demand by dependency-using routes."""
    configuration = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        logging.getLogger("app").setLevel(configuration.log_level)
        with resource_factory(configuration) as checks:
            with (
                queue_factory(configuration) as queue,
                repository_factory(configuration) as repository_service,
            ):
                application.state.checks = checks
                application.state.task_queue = queue
                application.state.repository_service = repository_service
                try:
                    yield
                finally:
                    del application.state.checks
                    del application.state.task_queue
                    del application.state.repository_service

    application = FastAPI(title=configuration.app_name, lifespan=lifespan)
    application.state.settings = configuration
    application.include_router(router)
    application.include_router(tasks_router)
    application.include_router(repositories_router)
    return application
