# Test suite and CI (Steps 27–28)

No production behavior is added by these steps. Tests are organized into:

* `tests/unit`: fast, service-free tests with fake providers and temporary Git repositories.
* `tests/integration`: component contracts against disposable PostgreSQL databases, Redis
  workers, local Git, and optional Docker sandboxes. Service tests explicitly opt in.
* `tests/e2e`: complete HTTP → Celery → agents → Docker → mock PR acceptance, including
  a failed test, debug repair, dependency release and correlated JSON logs.

Directory markers (`unit`, `integration`, `e2e`) are applied during collection. Unknown
markers are errors. Real GitHub/LLM tests must carry the `external` marker and are skipped
unless `--run-external` is passed. No real-provider tests are currently needed or included.
Normal tests use httpx mocks, FakeLLMProvider and deterministic embeddings. A shared guard
blocks external Python DNS lookups; localhost remains available for opted-in services.
This is a regression safeguard, not an operating-system firewall for subprocesses.
Git fixtures use temporary local repositories; publication tests never push to GitHub.

## Commands

From the repository root, after the existing Python 3.12 development setup:

```powershell
python -m pip install -e ".[dev]"
# Fast unit tests; no Docker, database, token or model API required:
python -m pytest tests/unit -q
# Default discovery: all tests, with service-dependent cases skipped unless enabled:
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m mypy
```

For local integration and full acceptance, use the existing development Compose setup.
The URL below matches its **default disposable development credentials only**. If you
customized Compose, set TEST_DATABASE_URL to your local test-server URL instead.
The role must be able to create databases and enable pgvector. Tests create and remove
only UUID-named temporary databases. Never point this at a production server.

```powershell
docker compose up -d --wait --wait-timeout 180
docker pull python:3.12-slim
$env:TEST_DATABASE_URL = 'postgresql+psycopg://platform_dev:local-development-only@127.0.0.1:5432/platform_dev'
$env:REDIS_URL = 'redis://127.0.0.1:6379/0'
$env:CELERY_BROKER_URL = 'redis://127.0.0.1:6379/0'
$env:CELERY_RESULT_BACKEND = 'redis://127.0.0.1:6379/1'
$env:RUN_DATABASE_TESTS = '1'
$env:RUN_WORKER_TESTS = '1'
$env:RUN_SANDBOX_TESTS = '1'
$env:RUN_INFRASTRUCTURE_TESTS = '1'
# Integration tests:
python -m pytest tests/integration -q
# Complete fixture workflow:
python -m pytest tests/e2e -q
# All tests with every local service category enabled:
python -m pytest -q
```

Use a dedicated local Redis instance for tests. Tests use unique queues and local Git
identities; do not run service suites concurrently against the same Redis instance.
Omit RUN_INFRASTRUCTURE_TESTS when using service containers rather than this repository's
Compose configuration. Close the terminal or remove the RUN_* variables to restore
default skipping. No GITHUB_TOKEN or LLM_API_KEY is required.

## Coverage map

| Area | Principal tests |
| --- | --- |
| Configuration, health/readiness | unit/test_application.py |
| Models, UUIDs, constraints, migrations, transactions | integration/test_database.py; unit/test_session.py |
| GitHub client and error handling | unit/test_github.py |
| Workspaces, safe Git and patches | unit/test_workspace.py, test_publication_git.py, test_code_patches.py |
| Scanning, chunking, incremental persistence | unit/test_indexing.py, test_fixture_pipeline.py; integration/test_indexing.py |
| BM25, embeddings, vector and hybrid retrieval | unit/test_bm25.py, test_embeddings.py, test_hybrid.py; integration/test_bm25.py, test_vectors.py, test_hybrid.py |
| Issue context | unit/test_issue_context.py; integration/test_issue_context.py |
| DAG validation and planner | unit/test_planning.py, test_planner.py; integration/test_planning.py, test_planner.py |
| Dependency scheduler and duplicate delivery | unit/test_orchestration.py; integration/test_executions.py |
| Docker limits, cleanup, output and timeout | unit/test_sandbox.py; integration/test_sandbox.py |
| CodingAgent | integration/test_coding.py, test_coding_audit.py |
| TestAgent and command policy | unit/test_test_policy.py; integration/test_test_agent.py |
| DebugAgent, bounded retry and failure history | integration/test_recovery.py |
| ReviewAgent and mechanical gates | unit/test_review.py; integration/test_review.py |
| Git/PR generation, collisions, partial failure | unit/test_publication_git.py; integration/test_publication.py |
| Idempotency and crash/restart recovery | unit/test_reliability.py; integration/test_reliability.py, test_reliability_audit.py |
| Complete workflow and observability | integration/test_workflow.py; e2e/test_workflow.py |

`local_repository` provides a committed temporary Git checkout. `source_repository`
extends it with Python, TypeScript, README, ignored and binary files. The workflow fixture
composes real services with deterministic provider outputs, preserving actual agent,
patch, scheduler and publication behavior rather than replacing the workflow with a stub.

## GitHub Actions

`.github/workflows/ci.yml` runs on push and pull_request, using Python 3.12 on Ubuntu.
The unit job installs the editable development package, caches pip downloads keyed by
pyproject.toml, and runs Ruff, formatting, mypy and unit tests. The integration job uses
healthy PostgreSQL 16/pgvector and Redis service containers, then runs database/worker
tests and Docker/end-to-end acceptance after pulling the trusted Python image.

Docker runs only on GitHub-hosted ephemeral Linux runners. Fixture commands retain the
sandbox's non-root user, disabled network and resource limits. No privileged container,
production secret, real model key or personal GitHub token is configured. Checkout has
read-only permissions and does not persist credentials. Do not change this workflow to
pull_request_target or run untrusted pull requests on a sensitive self-hosted runner.
CI service passwords are disposable placeholders. Compose-specific infrastructure tests
are checked locally, because CI service containers do not use Compose.

The workflow has job timeouts and cancels superseded runs. Hosted execution is confirmed
only after GitHub runs the committed workflow; local checks cannot certify runner status.
Service setup follows [GitHub's service-container documentation](https://docs.github.com/en/actions/tutorials/use-containerized-services/create-postgresql-service-containers).
