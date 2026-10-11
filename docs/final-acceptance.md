# Final project acceptance evidence

Follow-up: the [October 9 synthetic scale benchmark](benchmark-results.md) now records
indexing, query latency and Python peak memory at 1,000 and 5,000 files. It supersedes the
absence-of-benchmark statements in this original audit below. The original full-suite counts
are historical, before that opt-in benchmark was added. Real-provider integration is now
authorized but remains blocked on local credentials and a disposable target repository;
hosted CI and release have not yet been confirmed.

Scope: implementation and automated acceptance of the supplied two-sentence project
description. PASS means code plus the cited automated checks support the capability, not
that model quality, production readiness, scale, or every real provider was certified.
Existing README/development edits are retained. No product functionality was added.

## Evidence conventions

API paths below are real routes. `WF` means POST `/workflows` and GET `/workflows/{id}`;
the component is internal to that workflow, not a separate public agent endpoint.
`EX` means GET `/executions/{id}`. Standalone POST `/plans/{id}/executions` creates/reconciles
scheduling state; only WF composes coding, tests, recovery, review and publication.
`CC` means CodeChunk; `AR` AgentRun; `TE` TaskExecution; `ER` ExecutionRun.
Test result evidence is stored in AR JSON metadata; there is no separate TestResult table.

Manual column: `None` means no external account needed to reproduce automated evidence.
`GH` means an authorized real GitHub read check would validate a specific token/repository;
`AI` means an opt-in real provider call would validate compatibility and output quality;
`PR` means an explicitly authorized disposable repository publication would validate actual
GitHub push/PR permissions. GH/AI/PR were not performed and are not prerequisites for the
code-level PASS. `Scale` is missing acceptance evidence, detailed below. Multi-host worker
deployment is not certified by a local-worker test.

## Requirement-by-requirement table

| Claim | Result | Implementation files and symbols | Database entities | API entry/status | Automated evidence | Manual integration / limits |
| --- | --- | --- | --- | --- | --- | --- |
| A. Multi-agent architecture | PASS | [workflow_stages.py](../app/orchestration/workflow_stages.py) `WorkflowStages.run`; [agents/](../app/agents/) PlannerAgent, CodingAgent, TestAgent, DebugAgent, ReviewAgent | WorkflowRun, ImplementationPlan, PlanTask, ER, TE, AR | WF, EX | [workflow integration](../tests/integration/test_workflow.py), [E2E](../tests/e2e/test_workflow.py) | AI; TestAgent is deterministic policy, not an LLM. Tasks within one execution are serialized. |
| B. GitHub issue ingestion | PASS | [client.py](../app/integrations/github/client.py) `HttpGitHubClient`; [repositories.py](../app/services/repositories.py) `RepositoryService.register/import_issue` | Repository, Issue; repository/issue uniqueness | POST `/repositories`; POST `/repositories/{repository_id}/issues/{issue_number}/import`; GET `/issues/{issue_id}` | [GitHub tests](../tests/unit/test_github.py), [database tests](../tests/integration/test_database.py), E2E | GH; mocked HTTP proves contracts/errors, not a particular token's access. |
| C. Dependency-aware planning | PASS | [planner.py](../app/agents/planner.py) `PlannerAgent.propose`; [plans.py](../app/services/plans.py) `PlanService.create` | ImplementationPlan, PlanTask, Issue | POST `/issues/{issue_id}/plan`; GET `/plans/{plan_id}`; WF | [planner unit](../tests/unit/test_planner.py), [planner integration](../tests/integration/test_planner.py) | AI; task quality is not guaranteed by schema validity. |
| D. DAG validation | PASS | [planning.py](../app/schemas/planning.py) `ImplementationPlanProposal`; [plan_dependencies.py](../app/services/plan_dependencies.py) `execution_order` | ImplementationPlan, PlanTask | Plan API above; scheduling via EX | [DAG unit](../tests/unit/test_planning.py), [persistence](../tests/integration/test_planning.py), [scheduler](../tests/integration/test_executions.py) | None; duplicate/missing/self dependencies and cycles rejected before persistence. |
| E. BM25 search | PASS | [bm25.py](../app/retrieval/bm25.py) `BM25Retriever.search`; [tokenization.py](../app/retrieval/tokenization.py); [postgres.py](../app/retrieval/postgres.py) `PostgresChunkSource.load` | Repository, CC | POST `/repositories/{repository_id}/search/bm25` | [unit](../tests/unit/test_bm25.py), [HTTP/Postgres](../tests/integration/test_bm25.py) | None; repository corpus is loaded per search; not proven at large scale. |
| F. Vector semantic retrieval | PASS | [vector.py](../app/retrieval/vector.py) `VectorRetriever.search`; [embeddings.py](../app/indexing/embeddings.py) `EmbeddingService.generate`; [OpenAI adapter](../app/integrations/llm/openai_embeddings.py) `OpenAIEmbeddingProvider.embed` | CC vectors/status/profile/hash, Repository | POST `/repositories/{repository_id}/search/vector` | [provider tests](../tests/unit/test_embeddings.py), [pgvector integration](../tests/integration/test_vectors.py) | AI; actual cosine queries and deterministic ranking tested; real-model relevance not evaluated. |
| G. Hybrid retrieval | PASS | [hybrid.py](../app/retrieval/hybrid.py) `HybridRetrievalService.search`; [issue_context.py](../app/services/issue_context.py) `IssueContextService.build` | CC, Repository, Issue | POST `/repositories/{repository_id}/search`; GET `/issues/{issue_id}/context` | [RRF unit](../tests/unit/test_hybrid.py), [integration](../tests/integration/test_hybrid.py), [context](../tests/integration/test_issue_context.py) | AI for production query vectors; equal-weight RRF, deduplication and repository isolation tested. Queries are per repository, not federated across all repositories. |
| H. Repository indexing | PASS | [scanner.py](../app/indexing/scanner.py) `RepositoryScanner.scan`; [chunking.py](../app/indexing/chunking.py) `chunk_source`; [service.py](../app/indexing/service.py) `IndexingService.index` | Repository, CC | WF; GET `/repositories/{repository_id}`; internal `repository.index_code` task (no index HTTP route) | [scanner/chunks](../tests/unit/test_indexing.py), [reconciliation/worker](../tests/integration/test_indexing.py) | None for local fixtures; GH for real checkout. Changes/deletions/unchanged IDs are tested. |
| I. Reasonably large repositories | PARTIAL | Scanner file limits and deterministic chunking; `EmbeddingService.generate` selects bounded batches. `IndexingService.index/_persist` accumulates all new/existing chunks; `PostgresChunkSource.load` loads all search documents | Repository, CC | Same indexing/search APIs | Indexing/embedding tests establish correctness and batching only | Scale: no defined repository-size target, memory/latency measurements or large-corpus benchmark; no bounded-memory source reconciliation or ANN index. |
| J. Redis-backed workers | PASS | [factory.py](../app/workers/factory.py) `create_celery_app`; [workflow_resources.py](../app/services/workflow_resources.py) `CeleryWorkflowQueue.enqueue`; [workflow.py](../app/workers/workflow.py) `advance_workflow` | WorkerDelivery, WorkflowRun, ER/TE/AR; Redis transport/results | WF; POST `/tasks/ping`, GET `/tasks/{task_id}` | [worker integration](../tests/integration/test_worker.py), E2E; prior onboarding also exercised an independent worker process | None locally; multi-host operation requires shared workspace configuration and separate operational validation. |
| K. Linux Docker isolation | PASS | [docker.py](../app/sandbox/docker.py) `DockerSandbox.execute`; [policy.py](../app/sandbox/policy.py); [runner.py](../app/sandbox/runner.py) `DockerCLI` | AR stores test outcomes; sandbox itself has no table | WF; no arbitrary-command API | [mock controls](../tests/unit/test_sandbox.py), [real container inspection](../tests/integration/test_sandbox.py), [real TestAgent](../tests/integration/test_test_agent.py) | None with local Linux Docker/pre-pulled image; container boundary is not a hostile multi-tenant guarantee. |
| L. CodingAgent | PASS | [coding.py](../app/agents/coding.py) `CodingAgent.propose`; [coding service](../app/services/coding.py) `CodingService.run`; [code_patches.py](../app/services/code_patches.py) `CodePatchService.apply` | AR, TE, ER | WF, EX | [patch tests](../tests/unit/test_code_patches.py), [coding integration](../tests/integration/test_coding.py), [adversarial patches](../tests/integration/test_coding_audit.py) | AI; safe patch handling tested, arbitrary issue-solving quality not established. |
| M. TestAgent | PASS | [testing agent](../app/agents/testing.py) `TestAgent.select`; [testing service](../app/services/testing.py) `TestService.run` | AR test reports, TE | WF, EX | [policy tests](../tests/unit/test_test_policy.py), [real/fake sandbox](../tests/integration/test_test_agent.py) | None; requires operator-selected meaningful commands/images. Zero-discovered-tests success is not rejected generically. |
| N. DebugAgent | PASS | [debugging.py](../app/agents/debugging.py) `DebugAgent.propose`; [recovery.py](../app/services/recovery.py) `RecoveryService._debug` | AR, TE | WF, EX | [recovery integration](../tests/integration/test_recovery.py), E2E | AI; fixture repairs deterministic, not an empirical real-model success rate. |
| O. ReviewAgent | PASS | [review.py](../app/agents/review.py) `ReviewAgent.review`; [review service](../app/services/review.py) `ReviewService.run`; [review_gate.py](../app/orchestration/review_gate.py) `test_evidence` | AR, TE, ER | WF, EX | [review unit](../tests/unit/test_review.py), [review integration](../tests/integration/test_review.py) | AI; model approval cannot override failed/stale required tests or blocking findings. Human review still needed. |
| P. Automatic test-failure recovery | PASS | [recovery.py](../app/services/recovery.py) `RecoveryService.run`; [recovery_records.py](../app/services/recovery_records.py) `RecoveryRecords` | Recovery/debug/test ARs, TE, ER | WF, EX | [recovery scenarios](../tests/integration/test_recovery.py), E2E intentional first failure/repair | None for fake-provider proof; recovery can exhaust and fail. |
| Q. Bounded retries | PASS | `RecoveryService.run`; [planner.py](../app/agents/planner.py) `PlannerAgent.propose`; [failures.py](../app/orchestration/failures.py); [config.py](../app/core/config.py) `Settings` | Persisted AR attempts, WorkflowRun attempts/history, TE | WF status/resume | [recovery bounds](../tests/unit/test_recovery_config.py), [recovery integration](../tests/integration/test_recovery.py), [reliability](../tests/integration/test_reliability.py) | None; defaults include three debug attempts; provider/workflow limits are separate. |
| R. Git branches | PASS | [git.py](../app/pull_requests/git.py) `LocalPublicationGit.prepare`; [service.py](../app/pull_requests/service.py) `PullRequestService.run` | ER.branch_name, publication AR | WF with publication enabled | [Git unit](../tests/unit/test_publication_git.py), [publication integration](../tests/integration/test_publication.py) | PR for remote permissions; local real-Git branch creation/collisions tested. |
| S. Git commits | PASS | `LocalPublicationGit.prepare`; [body.py](../app/pull_requests/body.py) publication summaries | Publication AR, ER | WF publication | Same Git/publication tests assert commit, exact files and repeat safety | PR for real push; local commits are actual Git objects, not fake commits. |
| T. Review-ready PR generation | PASS | [service.py](../app/pull_requests/service.py) `PullRequestService.run`; [records.py](../app/pull_requests/records.py) `PublicationRecords.check/journal/finish`; [client.py](../app/integrations/github/client.py) PR adapter; [body.py](../app/pull_requests/body.py) | PullRequest, publication/review/test AR, ER | WF with `publish_pull_request=true`; status returns PR URL. No standalone PR route | [publication](../tests/integration/test_publication.py), E2E checks issue/plan/files/tests/review and one mock PR | PR explicitly opt-in, not performed. PASS means gated PR preparation/payload/reconciliation, not guaranteed code quality or proven live GitHub publication. |
| U. PostgreSQL persistence | PASS | [models/](../app/models/), [session.py](../app/db/session.py) `session_scope/create_database_engine`; [migrations](../migrations/versions/) | All domain entities, including WorkerDelivery and WorkflowRun | Persistent API routes | [database/migrations](../tests/integration/test_database.py), [session tests](../tests/unit/test_session.py) | None locally; backup, restore, HA and production operations not certified. |
| V. pgvector | PASS | [repository model](../app/models/repository.py) CC vector field; Alembic revisions; `VectorRetriever.search` | CC embedding vector(1536) | Vector/hybrid search | [infrastructure](../tests/integration/test_infrastructure.py), database and vector integration | None locally; changing dimensions requires a migration. No ANN index configured. |
| W. FastAPI | PASS | [main.py](../app/main.py) `create_app`; [api/](../app/api/) routes and correlation middleware | Service-owned persistence | `/health`, `/ready`, repositories/issues/search/context/plans/executions/workflows/tasks; `/docs`, `/openapi.json` | [application tests](../tests/unit/test_application.py), integration route tests, E2E | None; readiness checks PostgreSQL/Redis only. |
| X. Failure handling | PASS | [transitions.py](../app/orchestration/transitions.py), [workflow_records.py](../app/orchestration/workflow_records.py), [failures.py](../app/orchestration/failures.py); provider exception adapters | WorkflowRun, WorkerDelivery, AR, TE, ER | WF status/resume/cancel; EX cancel; GET `/worker-deliveries/{delivery_id}` | [failure injection](../tests/integration/test_reliability_audit.py), [reliability](../tests/integration/test_reliability.py), recovery/provider tests | None for tested injections; ambiguous external effects may block for inspection. Cancellation is not process termination. |
| Y. Idempotency | PASS | `WorkflowRecords.start/claim`; [deliveries.py](../app/orchestration/deliveries.py) `DeliveryRecords.execute`; `PublicationRecords` and `PullRequestService.run`; import uniqueness | WorkflowRun request ID/generation, WorkerDelivery task ID, repository/issue constraints, publication journals | WF request_id, imports and worker/publication replay | Workflow, indexing, database, publication and reliability tests | None for local replay proof; no global exactly-once guarantee. New task IDs/direct plan/execution submissions may intentionally create new work. |
| Z. Security controls | PASS | [policy.py](../app/sandbox/policy.py), `DockerSandbox`, `CodePatchService`, `TestAgent.select`; [observability.py](../app/core/observability.py) redaction; [Git runner](../app/integrations/git/runner.py) credential helper | AR records sanitized failures; persisted source/evidence remain sensitive | Controls apply through services; no built-in API auth layer | Sandbox inspection, patch traversal, command-injection, workspace and [redaction](../tests/unit/test_observability.py) tests | None for implemented controls; see residual risks below. PASS does not mean production-secure or penetration-tested. |

## Exact gaps and scope limits

**I is PARTIAL.** Chunked content and batched embedding requests exist, but that alone does
not establish acceptable behavior on a large repository. Source indexing builds a list of
all chunks and reads all existing chunks for reconciliation; BM25 loads the corpus and
recomputes ranking structures per query. Vector retrieval lacks an ANN index. There is no
recorded size target, realistic large-corpus benchmark, peak-memory measurement, latency
budget or throughput result. Required acceptance work: define representative file/chunk
counts and a hardware/resource budget; benchmark initial/incremental indexing and lexical,
vector and hybrid query latency; address measured failures, then retain regression evidence.
Do not claim bounded-memory source indexing or proven large-repository scale today.

All retrieval is explicitly repository-scoped. “Across repositories” can mean operating on
multiple registered repositories separately; there is no global cross-repository query API.

The architecture has specialized agents, not five independent autonomous LLM systems:
TestAgent is deterministic trusted-command selection. Independent DAG tasks are recognized,
but one workflow runs implementation tasks serially in its shared workspace. Redis/Celery
supports multiple worker processes; multi-host storage/locking and load were not validated.

No live GitHub push/PR or real model call was authorized or performed. HTTP contracts,
provider validation/retries and publication reconciliation are tested with mocks; deterministic
fake vectors prove retrieval plumbing, not real-model semantic quality. Optional live tests
would require local GITHUB_TOKEN/LLM_API_KEY and a disposable authorized repository. Never
put credentials in this report/chat. Manual configuration instructions are in
[development.md](development.md#github-and-llm-setup).

Security gaps remain: no API authentication/tenant authorization/rate limiting, no production
deployment boundary, no universal secret detection, no prompt-injection guarantee, and no
hostile multi-tenant sandbox certification. Test success does not prove meaningful test
coverage (unittest can discover zero tests) or prevent malicious repositories weakening tests.
Use trusted images and required-test policy, dedicated workers, external access controls and
human PR review. These limits qualify Z and “review-ready”; they are not hidden guarantees.

## Verification run

The full run completed successfully against revision `0c25da7` with documentation-only
working-tree changes. The report was finalized on October 9, 2026. Local PostgreSQL,
Redis workers and Linux Docker were enabled; GitHub/LLM providers were mocked.

| Suite | Total | Passed | Failed/errors | Skipped |
| --- | ---: | ---: | ---: | ---: |
| Unit | 325 | 325 | 0 | 0 |
| Integration | 160 | 160 | 0 | 0 |
| End-to-end fake-provider workflow | 1 | 1 | 0 | 0 |
| Total | 486 | 486 | 0 | 0 |

Elapsed pytest time: 650.79 seconds. Two existing Starlette/httpx/AnyIO deprecation
warnings remain; no tests failed or were skipped. Ruff lint and formatting passed.
mypy reported no issues in 198 source files. All report evidence links resolve.

Commands executed from the Python 3.12 development environment:

```powershell
# Existing local test database/Redis URLs configured; all four local flags enabled:
# RUN_DATABASE_TESTS, RUN_WORKER_TESTS, RUN_SANDBOX_TESTS, RUN_INFRASTRUCTURE_TESTS = 1
python -u -m pytest -q --junitxml=.pytest_cache/final-acceptance.xml
python -m ruff check .
python -m ruff format --check .
python -m mypy
```

Full local output: `.pytest_cache/final-acceptance.log`; machine-readable counts:
`.pytest_cache/final-acceptance.xml`. These generated files are ignored by Git.

## Acceptance conclusion

**25 criteria PASS at the implementation/automated-test level; I is PARTIAL; none FAIL.**
Overall acceptance of the exact resume wording is **PARTIAL**, despite a fully passing
test suite. The issue-to-plan, hybrid retrieval, Redis/Celery execution, Linux sandbox,
agent coordination, bounded recovery and gated PR-generation mechanisms are supported.
The phrase **“across large repositories” is not yet substantiated by scale evidence**.
Remove that qualifier or complete the measured acceptance work above before using the
description without qualification. Real-provider semantic quality and live GitHub
publication were not certified; no real credentials, model calls or remote PRs were used.
