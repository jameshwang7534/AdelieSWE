# Implemented architecture

The system is an `app/` Python package, with an API process, Celery workers and one
Celery Beat scheduler. PostgreSQL owns durable domain state; Redis carries jobs and
short-lived Celery results. API and worker imports do not eagerly connect to the database.
Factories and lifespan/context managers own service resources.

## Data and service boundaries

SQLAlchemy models include Repository, Issue, CodeChunk, ImplementationPlan, PlanTask,
ExecutionRun, TaskExecution, AgentRun, PullRequest, WorkflowRun and worker delivery records.
UUID keys, foreign keys, constraints, timezone-aware timestamps and JSONB metadata support
the workflow. Repository and GitHub issue uniqueness prevent duplicate imports. Alembic
migrations are the schema authority; the pgvector column currently uses 1536 dimensions.

HTTP handlers delegate to services. GitHub HTTP is centralized behind GitHubClient/httpx;
Git commands use a subprocess abstraction. LLMProvider supports asynchronous validated
structured responses; EmbeddingProvider supports batched vectors. Fake implementations
are injected in tests. The running API does not select a fake provider via an environment
variable. DockerSandbox implements the Sandbox contract using a bounded CLI runner.

## Retrieval and context

1. WorkspaceService prepares managed repository/execution copies with path and Git checks.
2. RepositoryScanner skips explicit irrelevant/credential/generated paths, large files,
   binary/non-UTF-8 content and suspicious secret patterns. It does not interpret `.gitignore`.
3. Deterministic line chunks, with Python function boundaries where practical, retain
   path/language/start/end/content/hash. Reindexing preserves unchanged records, replaces
   changed chunks and removes obsolete chunks.
4. Embedding generation batches missing/stale vectors, validates dimensions and persists
   provider/model/content identity. Successful batches survive later failures.
5. BM25 performs identifier-friendly lexical matching. Vector retrieval computes cosine
   distance in PostgreSQL with repository filtering. Hybrid retrieval merges candidates
   by equal-weight Reciprocal Rank Fusion: `sum(1 / (60 + rank))`, with missing ranks
   contributing zero. Candidate depth defaults to at least 50 per retriever; chunk IDs
   deduplicate results, with deterministic tie-breaking. Raw scores are not added.
6. IssueContextService derives queries without an LLM, retrieves hybrid candidates and
   optional neighbors, deduplicates and enforces file/chunk/character/query bounds.

BM25 is not a persistent standalone search server. Retrieval calls can observe different
committed snapshots during concurrent reindexing. Hybrid search requires a usable vector
provider; an empty candidate list differs from a provider configuration or network error.

## Planning, execution and publication

PlannerAgent outputs ImplementationPlanProposal. Pydantic validation and the DAG validator
reject empty plans, duplicate task keys, missing/self dependencies and cycles. Invalid
proposals receive bounded feedback retries and are never persisted as executable plans.

The complete workflow stages are repository → sync → index → embed → issue → context →
plan → execution → workspace → implement (repeated per task) → review → publish/done.
Publication is skipped when the original request disables it. `/workflows` owns this
composition; the standalone `/plans/{id}/executions` endpoint owns scheduling state only.

Task readiness depends on persisted dependency outcomes. The workflow selects one ready
task at a time to avoid conflicting writes in a shared execution workspace. CodingAgent
applies a validated patch; TestAgent selects trusted command profiles and runs Docker.
DebugAgent receives failure evidence and prior attempts, and proposes another safe patch
within the retry budget. Required tests must pass before task completion. ReviewAgent
findings cannot override mechanical task/test/publication requirements.

PR publication checks current workspace changes and review evidence, uses a deterministic
`ai-platform/issue-{number}-{execution-id}` branch, commits and pushes without force-pushing
the default branch, and reconciles existing branches/PRs after partial failures. It records
the PR locally and does not merge it. Publication is explicitly enabled per workflow.

## Queues, checkpoints and monitoring

* `orchestration`: workflow control, repository preparation, reconciliation, recovery and diagnostics.
* `indexing`: source and embedding stages/standalone tasks.
* `agents`: plan, implement/recovery and review workflow stages.

`workflow.advance` claims one persisted stage; generation/claim guards reject obsolete
deliveries. PostgreSQL transactions hold state changes; provider calls and filesystem/
Docker work are not a distributed transaction. Late acknowledgments and Beat recovery
help recover lost dispatches. Replays avoid completed work where safe; ambiguous
side effects may block rather than retry. See [reliability](orchestration-reliability.md).

`GET /workflows/{id}` is the overall status, including review/publication. Execution status
shows task outcomes, attempts and elapsed time. AgentRun records hold per-agent evidence.
JSON logs include available correlation/entity/Celery IDs and timing; successful transition
events are emitted after commit. See [observability](observability.md).
