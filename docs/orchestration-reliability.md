# Orchestration reliability (Step 25)

PostgreSQL owns work identity and state. Redis notifications can be repeated or lost;
Celery Beat recovers committed pending workflow rows. Run Beat alongside workers.

## Reviewed distributed operations

| Operation | Duplicate/retry behavior | Crash or uncertain outcome |
| --- | --- | --- |
| system.ping | Side-effect-free; at most three Celery retries with backoff | Redis result may expire; submit another diagnostic |
| execution.reconcile/recover | Run-row lock, validated task transitions, completed tasks stay terminal | Beat repeats reconciliation; stale active tasks fail closed |
| repository prepare/index/embed | Durable WorkerDelivery keyed by Celery UUID; completed result reused; concurrent delivery does not invoke work | Thirty-minute claim expires to failed; inspect before a new explicitly submitted task |
| workflow.advance | Workflow lock, claim token, stage and generation fence | Thirty-minute lease invalidates old checkpoint writes |
| workflow.recover | Pending DB row survives broker outage; notification errors are logged and other rows continue | Next Beat pass rediscovers due rows |
| GitHub repository/issue reads | Transient failures retry in safe workflow stages with persisted deadlines | Authentication/not-found/configuration errors require correction and explicit resume |
| Context/planner LLM | Provider timeout/retry remains bounded; workflow transient retry is additionally bounded | Deterministic plan ID reuses a plan committed before checkpoint failure |
| Coding/test/debug/review | Existing claims, patch locks, validation gates and bounded debug attempts | Never automatically replay potentially applied patches; interrupted workflow is blocked |
| Branch/push/PR | Existing publication journal, branch SHA checks, PR lookup and unique DB execution record | Explicit resume reconciles remote state; never blindly retry POST or push |

All standalone repository deliveries have **one operation attempt per Celery task ID**.
New task IDs represent new explicit requests (for example, synchronize a changed repository).
Source indexing preserves unchanged chunks; embedding generation reuses completed chunk vectors.
Cross-task filesystem locks prevent simultaneous writes to the same managed copy. A failed
database checkpoint can still mean the external operation succeeded: that delivery is not
automatically replayed. Exactly-once external effects cannot be guaranteed across PostgreSQL,
GitHub, Docker, and the filesystem.

## States and retries

Guarded transitions are centralized in `app/orchestration/transitions.py`. Record services
own transactions and domain preconditions; callers do not directly set task/agent/run status.
Completed/failed/cancelled task states cannot return to running. The intentional exception is
a failed publication journal becoming completed after remote reconciliation proves success.
Failed workflow stages may explicitly resume within their budget; blocked stages cannot.

`WORKFLOW_STAGE_ATTEMPTS` (default 3) bounds claims per checkpoint. Transient GitHub/LLM/network
timeouts in repository, issue, context, and plan stages use `WORKFLOW_RETRY_SECONDS` (default 30)
with exponential backoff, capped at one hour. Retry-After up to one hour is respected. Longer
rate-limit waits require manual resume. `retry_at`, `generation`, `attempts`, `error_code`, and
history are returned by GET /workflows/{id}. Error history is preserved across retries.
Source-changing stages and unclassified errors are never automatically retried.

Cancellation endpoints:

* `POST /workflows/{id}/cancel`: cancels pending/failed/blocked workflow at a safe boundary.
* `POST /executions/{id}/cancel`: cancels unfinished idle tasks; keeps completed work intact.
* Repeating cancellation is safe. Active agents/tasks or an active workflow stage return 409.
* Cancellation does not kill processes or retract remote commits/PRs. It prevents future claims.

An expired implementation workflow marks active AgentRuns failed, running tasks failed, and
unfinished downstream tasks blocked in the same transaction that blocks the workflow. Late
agent completions cannot restore these records. Independent execution recovery uses
`EXECUTION_STALE_SECONDS` (default 3600, minimum 1800) and rejects work that exceeded its bound.
The normal worker hard limit is 960 seconds. Completed task executions remain completed even
when a later review/publication agent fails; workflow status describes overall completion.

Inspect standalone repository deliveries with `GET /worker-deliveries/{celery-task-uuid}`.
The durable result/failure remains inspectable after Redis result expiry. An interrupted delivery
may leave its repository progress flag or filesystem lock unchanged: inspect the delivery error
and ensure the original process is dead before repairing a lock or submitting a replacement.
Recovery deliberately does not delete filesystem locks or claim it can undo external effects.

## Database/broker failures and deployment

Transactions roll back failed claims/state updates. If checkpoint commit fails after effects,
the running claim is retained until lease recovery rather than rerunning the operation. Database
connections use pool pre-ping. Broker startup/reconnect, publishing, and result storage retain
bounded retries. Late-ack workers cancel running tasks on broker connection loss where Celery's
pool supports termination. Windows solo pool cannot guarantee termination/time limits: verify
the original process has stopped before manual recovery. This is not a multi-host fencing lock.

Migration 0006 adds workflow generations/retry deadlines/cancelled state and worker_deliveries.
Stop old API/worker/Beat processes, run `python -m alembic upgrade head`, and restart them together.
Legacy two-argument workflow messages remain accepted; new producers include generation.
Drain old messages during deployment to obtain generation fencing for every delivery.

Tests use fake GitHub/LLM providers, fixture Git repositories and isolated PostgreSQL databases.
No real provider key or PR is needed. Enable RUN_DATABASE_TESTS/TEST_DATABASE_URL for persistence
tests; RUN_WORKER_TESTS and RUN_SANDBOX_TESTS enable live Redis/Docker coverage.

## Implementation verification

Final checks completed October 6, 2026:

| Check | Result |
| --- | --- |
| Default pytest suite | 317 passed, 157 opt-in skips |
| Reliability + live workflow + source-index worker | 13 passed |
| Embedding worker + reliability rerun | 11 passed |
| Workspace worker | Passed |
| Publication/coding/TestAgent regression suite | 28 passed, 5 Docker opt-in skips |
| Database migration upgrade/downgrade and metadata tests | Passed |
| Ruff lint, format, mypy, git diff whitespace | Passed |
| Local Alembic schema | 0006 head; no pending schema differences |

Two existing Starlette/httpx/anyio deprecation warnings remain. Failure injection covers
transaction rollback, checkpoint failure after an effect, duplicate/concurrent delivery,
stale ownership, broker notification failure/recovery, provider timeout, and bounded retries.
Real Redis and Docker were exercised, but Redis process termination and OS worker termination
were not injected; crash/reconnect behavior is tested through persisted stale claims and mocks.
No real GitHub write or model API call was made.

Commands used (Python is `.venv/Scripts/python.exe`):

```powershell
docker compose up -d --wait --wait-timeout 180
python -m pytest -q --tb=short
# With RUN_DATABASE_TESTS=1, RUN_WORKER_TESTS=1, RUN_SANDBOX_TESTS=1,
# and the existing local test database/broker configuration:
python -m pytest tests/integration/test_reliability.py tests/integration/test_workflow.py::test_api_redis_worker_workflow tests/integration/test_indexing.py -q --tb=short
python -m pytest tests/integration/test_vectors.py::test_embedding_worker tests/integration/test_reliability.py -q --tb=short
python -m pytest tests/integration/test_database.py::test_workspace_task_through_redis_worker -q
python -m ruff check .
python -m ruff format --check .
python -m mypy
git diff --check
python -m alembic upgrade head
python -m alembic check
python -m alembic current
```

Created files:

* `app/models/delivery.py`
* `app/orchestration/deliveries.py`, `failures.py`, `transitions.py`
* `app/workers/deliveries.py`
* `migrations/versions/0006_workflow_reliability.py`
* `tests/integration/test_reliability.py`, `tests/unit/test_reliability.py`
* `docs/orchestration-reliability.md`

Modified files:

* `.env.example`, `README.md`, `app/core/config.py`
* `app/api/executions.py`, `app/api/workflows.py`
* `app/models/__init__.py`, `app/models/workflow.py`, `app/schemas/workflows.py`
* `app/orchestration/workflow.py`, `app/orchestration/workflow_records.py`
* `app/pull_requests/records.py`
* `app/services/coding_records.py`, `executions.py`, `recovery_records.py`,
  `review_records.py`, `test_records.py`, `workflow_resources.py`
* `app/workers/factory.py`, `orchestration.py`, `workflow.py`
* `tests/integration/test_database.py`, `test_indexing.py`, `test_vectors.py`, `test_workflow.py`
* `tests/unit/test_workflow.py`, `tests/unit/test_workspace.py`

Local schema migration is applied. No additional credentials or software are required for
these tests. Restart any previously running application processes before using the new code.
Suggested commit: `feat: harden orchestration retries, delivery fencing, and cancellation`.
