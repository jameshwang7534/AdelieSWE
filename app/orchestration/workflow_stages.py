"""Production stage composition. All HTTP/model/Git/sandbox boundaries are injectable."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass, field
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.coding import CodingAgent
from app.agents.debugging import DebugAgent
from app.agents.planner import PlannerAgent
from app.agents.review import ReviewAgent
from app.core.config import Settings
from app.db.session import session_scope
from app.indexing.embeddings import EmbeddingService
from app.indexing.scanner import RepositoryScanner
from app.indexing.service import IndexingService
from app.integrations.github.client import GitHubClient
from app.integrations.llm.embeddings import EmbeddingProvider
from app.integrations.llm.provider import LLMProvider
from app.models import AgentRun, ExecutionRun, ImplementationPlan
from app.orchestration.workflow_records import WorkflowClaim, WorkflowError
from app.pull_requests.contracts import PublicationGit, PublicationGitHub
from app.pull_requests.records import PublicationRecords
from app.pull_requests.service import PullRequestService
from app.retrieval.bm25 import BM25Retriever
from app.retrieval.hybrid import HybridRetrievalService
from app.retrieval.postgres import PostgresChunkSource
from app.retrieval.vector import VectorRetriever
from app.sandbox.base import Sandbox
from app.schemas.context import IssueContext
from app.schemas.review import ReviewDecision
from app.schemas.testing import RepositoryTestConfig
from app.services.code_patches import CodePatchService
from app.services.coding import CodingService
from app.services.coding_records import CodingRecords
from app.services.context_store import PostgresContextStore
from app.services.executions import ExecutionService
from app.services.issue_context import IssueContextService
from app.services.plans import PlanService
from app.services.recovery import RecoveryService
from app.services.recovery_records import RecoveryRecords
from app.services.repositories import RepositoryService
from app.services.review import ReviewService
from app.services.review_records import ReviewRecords
from app.services.test_records import TestRecords
from app.services.testing import TestService
from app.services.workspace import WorkspaceService
from app.services.workspace_sync import prepare_registered_repository


@dataclass
class StageResult:
    data: dict[str, object] = field(default_factory=dict)
    repeat: bool = False


class WorkflowStages:
    def __init__(
        self,
        settings: Settings,
        sessions: sessionmaker[Session],
        github: GitHubClient,
        publication_github: PublicationGitHub,
        workspace: WorkspaceService,
        llm: Callable[[], AbstractAsyncContextManager[LLMProvider]],
        embeddings: Callable[[], AbstractContextManager[EmbeddingProvider]],
        sandbox: Sandbox,
        publication_git: PublicationGit,
    ) -> None:
        self.settings, self.sessions, self.workspace = settings, sessions, workspace
        self.repositories = RepositoryService(sessions, github)
        self.llm, self.embeddings, self.sandbox = llm, embeddings, sandbox
        self.publication_github, self.publication_git = publication_github, publication_git

    async def run(self, claim: WorkflowClaim) -> StageResult:
        data, stage = claim.data, claim.stage
        if stage == "repository":
            repository = self.repositories.register(
                claim.request.github_owner, claim.request.github_name
            )
            return StageResult({"repository_id": str(repository.id)})
        repository_id = UUID(str(data["repository_id"]))
        if stage == "sync":
            result = prepare_registered_repository(repository_id, self.sessions, self.workspace)
            return StageResult({"source_commit": result.commit})
        if stage in {"index", "embed", "context", "workspace"}:
            with self.workspace.locked_repository(repository_id) as root:
                if self.workspace.verify(root) != data["source_commit"]:
                    raise WorkflowError("workflow_repository_revision_changed")
        if stage == "index":
            result_index = IndexingService(
                self.sessions,
                self.workspace,
                RepositoryScanner(
                    self.settings.index_max_file_bytes, self.settings.index_chunk_max_chars
                ),
                max_lines=self.settings.index_chunk_max_lines,
                max_chars=self.settings.index_chunk_max_chars,
            ).index(repository_id)
            if result_index["commit"] != data["source_commit"]:
                raise WorkflowError("workflow_repository_revision_changed")
            return StageResult()
        if stage == "embed":
            with self.embeddings() as provider:
                EmbeddingService(
                    self.sessions, self.workspace, provider, self.settings.embedding_batch_size
                ).generate(repository_id)
            return StageResult()
        if stage == "issue":
            issue = self.repositories.import_issue(repository_id, claim.request.issue_number)
            return StageResult({"issue_id": str(issue.id)})
        issue_id = UUID(str(data["issue_id"]))
        if stage == "context":
            with self.embeddings() as provider:
                hybrid = HybridRetrievalService(
                    BM25Retriever(PostgresChunkSource(self.sessions)),
                    VectorRetriever(self.sessions, provider),
                )
                context = IssueContextService(
                    PostgresContextStore(self.sessions), hybrid, self.settings
                ).build(issue_id)
            return StageResult({"context": context.model_dump(mode="json")})
        context = IssueContext.model_validate(data["context"])
        if stage == "plan":
            identifier = uuid5(claim.id, "plan")
            with session_scope(self.sessions) as session:
                existing_plan = session.get(ImplementationPlan, identifier)
            if existing_plan is None:
                async with self.llm() as provider_llm:
                    proposal = await PlannerAgent(
                        provider_llm, self.settings.planner_validation_retries
                    ).propose(context)
                PlanService(self.sessions).create(issue_id, proposal, identifier=identifier)
            return StageResult({"plan_id": str(identifier)})
        if stage == "execution":
            identifier = uuid5(claim.id, "execution")
            with session_scope(self.sessions) as session:
                existing_run = session.get(ExecutionRun, identifier)
            if existing_run is None:
                ExecutionService(self.sessions).create(
                    UUID(str(data["plan_id"])), identifier=identifier
                )
            return StageResult({"execution_id": str(identifier)})
        run_id = UUID(str(data["execution_id"]))
        if stage == "workspace":
            path = self.settings.workspace_root / "executions" / str(repository_id) / str(run_id)
            if path.exists():
                commit = self.workspace.verify(path)
                with CodePatchService(self.settings.workspace_root).locked(path) as safe:
                    if CodePatchService(self.settings.workspace_root).status(safe) != "clean":
                        raise WorkflowError("workflow_workspace_not_clean")
            else:
                commit = self.workspace.create_execution(repository_id, run_id).commit
            if commit != data["source_commit"]:
                raise WorkflowError("workflow_repository_revision_changed")
            return StageResult()
        config = RepositoryTestConfig.model_validate(
            {"required_commands": data["required_commands"]}
        )
        if stage == "implement":
            execution = ExecutionService(self.sessions).reconcile(run_id)
            if execution.status == "completed":
                return StageResult()
            if execution.status == "failed" or any(
                task.status == "running" for task in execution.tasks
            ):
                raise WorkflowError("workflow_execution_requires_inspection")
            ready = next((task for task in execution.tasks if task.status == "queued"), None)
            if ready is None:
                raise WorkflowError("workflow_no_ready_task")
            async with self.llm() as provider_llm:
                coding = CodingService(
                    self.settings,
                    CodingAgent(provider_llm),
                    CodingRecords(self.sessions),
                    CodePatchService(self.settings.workspace_root),
                )
                recovery = RecoveryService(
                    self.settings,
                    coding,
                    TestService(self.settings, self.sandbox, TestRecords(self.sessions)),
                    DebugAgent(provider_llm),
                    RecoveryRecords(self.sessions),
                )
                report = await recovery.run(run_id, ready.id, context, config)
            if not report.passed:
                raise WorkflowError("workflow_tests_failed")
            return StageResult(repeat=True)
        if stage == "review":
            # Reuse a completed approval after a crash between review and workflow checkpoint.
            with session_scope(self.sessions) as session:
                review = session.scalars(
                    select(AgentRun)
                    .where(
                        AgentRun.execution_run_id == run_id,
                        AgentRun.agent_type == "review",
                    )
                    .order_by(AgentRun.started_at.desc(), AgentRun.id.desc())
                ).first()
                decision = (
                    ReviewDecision.model_validate(review.output_metadata.get("decision"))
                    if review and review.status == "completed"
                    else None
                )
            if decision is None:
                async with self.llm() as provider_llm:
                    decision = await ReviewService(
                        self.settings,
                        ReviewAgent(provider_llm),
                        ReviewRecords(self.sessions),
                        CodePatchService(self.settings.workspace_root),
                    ).run(run_id, context, config)
            if not decision.approved:
                raise WorkflowError("workflow_review_rejected")
            PublicationRecords(self.sessions).check(run_id, context, config)
            return StageResult()
        if stage == "publish":
            result_pr = PullRequestService(
                self.settings,
                PublicationRecords(self.sessions),
                self.publication_git,
                self.publication_github,
            ).run(run_id, context, config)
            return StageResult({"pull_request_url": result_pr.html_url})
        raise WorkflowError("workflow_unknown_stage")
