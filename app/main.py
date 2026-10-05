"""FastAPI factory with lifespan-owned dependency clients."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, asynccontextmanager

from fastapi import FastAPI

from app.api.context import router as context_router
from app.api.executions import router as executions_router
from app.api.plans import LLMFactory
from app.api.plans import router as plans_router
from app.api.repositories import router as repositories_router
from app.api.search import router as search_router
from app.api.status import router
from app.api.tasks import router as tasks_router
from app.api.workflows import router as workflows_router
from app.core.config import Settings
from app.orchestration.workflow_records import WorkflowRecords
from app.retrieval.base import Retriever
from app.retrieval.hybrid import HybridRetrievalService
from app.retrieval.vector import VectorRetriever
from app.services.context_resources import context_resources
from app.services.context_store import ContextStore
from app.services.execution_resources import execution_resources
from app.services.executions import ExecutionService
from app.services.github_resources import repository_resources
from app.services.issue_context import IssueContextService
from app.services.llm_resources import llm_resources
from app.services.plan_resources import plan_resources
from app.services.plans import PlanService
from app.services.readiness import Checks, readiness_resources
from app.services.repositories import RepositoryService
from app.services.retrieval_resources import retrieval_resources
from app.services.task_queue import TaskQueue, task_queue_resources
from app.services.vector_resources import vector_resources
from app.services.workflow_resources import WorkflowQueue, workflow_api_resources

ResourceFactory = Callable[[Settings], AbstractContextManager[Checks]]
QueueFactory = Callable[[Settings], AbstractContextManager[TaskQueue | None]]
RepositoryFactory = Callable[[Settings], AbstractContextManager[RepositoryService | None]]
RetrievalFactory = Callable[[Settings], AbstractContextManager[Retriever | None]]
VectorFactory = Callable[[Settings], AbstractContextManager[VectorRetriever | None]]
ContextFactory = Callable[[Settings], AbstractContextManager[ContextStore | None]]
PlanFactory = Callable[[Settings], AbstractContextManager[PlanService | None]]
ExecutionFactory = Callable[[Settings], AbstractContextManager[ExecutionService | None]]
WorkflowFactory = Callable[
    [Settings], AbstractContextManager[tuple[WorkflowRecords, WorkflowQueue] | None]
]


def create_app(
    settings: Settings | None = None,
    resource_factory: ResourceFactory = readiness_resources,
    queue_factory: QueueFactory = task_queue_resources,
    repository_factory: RepositoryFactory = repository_resources,
    retrieval_factory: RetrievalFactory = retrieval_resources,
    vector_factory: VectorFactory = vector_resources,
    context_factory: ContextFactory = context_resources,
    plan_factory: PlanFactory = plan_resources,
    llm_factory: LLMFactory = llm_resources,
    execution_factory: ExecutionFactory = execution_resources,
    workflow_factory: WorkflowFactory = workflow_api_resources,
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
                retrieval_factory(configuration) as retriever,
                vector_factory(configuration) as vector_retriever,
                context_factory(configuration) as context_store,
                plan_factory(configuration) as plans,
                execution_factory(configuration) as executions,
                workflow_factory(configuration) as workflow,
            ):
                application.state.checks = checks
                application.state.workflow_resources = workflow
                application.state.execution_service = executions
                application.state.plan_service = plans
                application.state.llm_factory = llm_factory
                application.state.task_queue = queue
                application.state.repository_service = repository_service
                application.state.bm25_retriever = retriever
                application.state.vector_retriever = vector_retriever
                application.state.retriever = (
                    HybridRetrievalService(retriever, vector_retriever)
                    if retriever is not None and vector_retriever is not None
                    else None
                )
                application.state.issue_context = (
                    IssueContextService(context_store, application.state.retriever, configuration)
                    if context_store is not None and application.state.retriever is not None
                    else None
                )
                try:
                    yield
                finally:
                    del application.state.checks
                    del application.state.task_queue
                    del application.state.repository_service
                    del application.state.bm25_retriever
                    del application.state.vector_retriever
                    del application.state.retriever
                    del application.state.issue_context
                    del application.state.plan_service
                    del application.state.llm_factory
                    del application.state.execution_service
                    del application.state.workflow_resources

    application = FastAPI(title=configuration.app_name, lifespan=lifespan)
    application.state.settings = configuration
    application.include_router(router)
    application.include_router(tasks_router)
    application.include_router(repositories_router)
    application.include_router(search_router)
    application.include_router(context_router)
    application.include_router(plans_router)
    application.include_router(executions_router)
    application.include_router(workflows_router)
    return application
