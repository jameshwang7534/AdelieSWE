# Step 25 failure-injection audit

Completed October 6, 2026. **PASS for all requested scenarios.**

| Scenario | Result | Evidence |
| --- | --- | --- |
| Same Celery task delivered twice | PASS | Real Redis/solo worker consumed the same message ID twice; diamond DAG retained exactly four task rows. Separately, 16 duplicate workflow stage deliveries produced no extra effects. |
| Worker exception after DB update | PASS | Exception after execution creation reused the deterministic execution ID on resume. Exception after plan checkpoint commit continued from the next persisted stage. |
| Worker exception before DB update | PASS | Injected pre-stage exception resumed safely. A failing before-commit hook rolled back a claim to pending with zero attempts. |
| Redis temporary failure | PASS | Injected queue ConnectionError retained pending rows. Recovery continued for other rows and redispatched both after the connection failure was removed. |
| LLM timeout | PASS | Mock HTTP timeouts and an actual async deadline produced sanitized llm_timeout; configured two-attempt workflow budget terminated without infinite retry. |
| GitHub timeout | PASS | Mock httpx.ReadTimeout mapped to github_unavailable; bounded workflow retries preserved a visible failure code. |
| Docker timeout | PASS | Real Linux container ran a sleeping test with a one-second limit. Timeout was recorded, container removed, task/test agent marked failed. |
| Execution reload after process restart | PASS | Two distinct Python processes loaded PostgreSQL state: A queued initially; after A completed, B/C queued and D pending. Four task rows remained. |
| Retry after completed work | PASS | Completed task claims returned no-op; completed stage messages did not run agents again. Completed publication reconciliation reused existing artifacts. |

## Duplicate accounting

The two-task end-to-end fixture injected exceptions before a context-stage update,
after execution creation, and after a plan checkpoint commit. Every stage delivery
was replayed with its original generation. Services were reconstructed from PostgreSQL
on each iteration. Final status was completed.

| Object | Actual | Expected | Duplicates |
| --- | --- | --- | --- |
| AgentRuns | 10 | 10 | 0 |
| TaskExecutions | 2 | 2 | 0 |
| ImplementationPlans | 1 | 1 | 0 |
| ExecutionRuns | 1 | 1 | 0 |
| Publication branches | 1 | 1 | 0 |
| Commits ahead of baseline | 1 | 1 | 0 |
| PullRequests | 1 | 1 | 0 |

The ten AgentRuns include two coding, three test, one debug, two recovery, one review,
and one publication run. The failed initial test and deliberate debug repair are
intended attempts, not duplicate records. The four-row diamond fixture is a separate test.

Publication tests also injected a lost push response, lost PR-create response, and a
failed local database update. Each reconciled to one publication AgentRun, one PR,
one branch, and one commit ahead of the baseline. Default HEAD remained unchanged.

## Executed checks

All commands ran from the repository root using `.venv/Scripts/python.exe`.
Database/worker tests used RUN_DATABASE_TESTS=1, RUN_WORKER_TESTS=1 and development
TEST_DATABASE_URL/Celery URLs; the Docker case also used RUN_SANDBOX_TESTS=1.

```powershell
python -m pytest tests/integration/test_reliability_audit.py tests/integration/test_reliability.py tests/integration/test_executions.py::test_duplicate_celery_delivery_and_recovery 'tests/integration/test_test_agent.py::test_real_docker_test_agent[timeout]' -q -s --tb=short
python -m pytest tests/integration/test_reliability_audit.py::test_execution_reloaded_in_new_process -q -s --tb=short
python -m pytest 'tests/integration/test_publication.py::test_publication[api-response-lost]' 'tests/integration/test_publication.py::test_publication[database-failure]' 'tests/integration/test_publication.py::test_publication[push-response-lost]' tests/unit/test_github.py tests/unit/test_llm_provider.py tests/unit/test_reliability.py -q --tb=short
python -m ruff check .
python -m ruff format --check .
python -m mypy
git diff --check
```

Results: **13 + 1 + 73 = 87 passing tests**, with two existing dependency deprecation
warnings per invocation. The process-reload test was added and run separately after
the first command. Ruff, formatting, mypy (189 files), and whitespace checks passed.

## Changes and limits

Created this report and `tests/integration/test_reliability_audit.py`. Enhanced
`tests/integration/test_publication.py` with branch/commit counts and
`tests/unit/test_github.py` with a ReadTimeout case. No production changes were needed.

Redis outage was simulated at the queue boundary; the Redis server was not stopped.
Fresh subprocesses proved reload, but a live worker was not forcibly killed. Docker
timeout was real. GitHub, LLM, push, and PR operations used mocks; no real PR was opened.
Test databases and repositories were isolated fixtures. No manual setup remains.

Suggested commit: `test: audit step 25 failure recovery and duplicate side effects`.
