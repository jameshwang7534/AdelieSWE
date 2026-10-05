# Step 24 end-to-end acceptance audit

Audited October 5, 2026. Result: **PASS**.

The synthetic issue was “Fix answer in calc.py”: return 2 and update the README.
Real PostgreSQL/pgvector, Redis/Celery, local Git, and DockerSandbox were used.
GitHub HTTP, branch push, LLM, and embedding providers were mocked. No real PR was opened.
The Celery test worker used a solo worker thread with real Redis delivery; this audit
does not demonstrate a separate OS worker process or multi-host operation.

## Recorded identities

| Record | ID |
| --- | --- |
| Workflow | `0574e420-9ec7-4a6b-9a60-854be83bdab8` |
| Imported issue | `23afadd3-1c0a-4a93-a68a-5dd897f4981e` |
| Plan | `93894283-5e76-5303-8e16-eb88c19545c2` |
| Execution | `6f5e6224-810e-5b29-b337-c076b7466275` |
| PullRequest | `5bf87530-f3d4-41d2-93a2-5c0189975e78` |

The PR record has number 1 and status `open`. Its mocked URL is
`https://github.com/fixture/repo-f4d6c1540b734353a700f5cc1af365b5/pull/1`.
These records were asserted in an isolated test database before teardown removed it.

## Acceptance evidence

| Requirement | Result | Observed evidence |
| --- | --- | --- |
| Repository preparation/indexing | PASS | Sync, source indexing, and embedding stages completed |
| Issue imported | PASS | Expected title persisted with the issue ID above |
| Hybrid context retrieved | PASS | Three snippets, including calc.py and vector-rank evidence |
| Implementation plan created | PASS | Task A fixes calc.py; task B documents the fix |
| Dependency DAG validated | PASS | Validated execution snapshot has order A, B; B depends on A |
| Execution created | PASS | Persisted execution and GET execution API report completed |
| Correct first task scheduled | PASS | A queued while B pending |
| CodingAgent changes files | PASS | Two coding runs; final exact contents and changed file set asserted |
| Docker tests execute | PASS | Three real DockerSandbox executions of python -m unittest discover |
| Debug loop | PASS | First coding patch returned 3; one debug patch changed it to 2 |
| Required tests pass | PASS | Exit codes 1, 0, 0; each run discovered one test |
| ReviewAgent runs | PASS | Approved, no findings, no mechanical errors |
| Branch and commit prepared | PASS | Actual local Git commit; default HEAD unchanged |
| Mock PR created and persisted | PASS | Exactly one push-adapter call, mocked PR, and database PR record |
| Duplicate start | PASS | Repeated POST reused the same workflow ID |

Observed scheduler snapshots:

```text
Before A: A=queued,    B=pending
After A:  A=completed, B=queued
Before B: A=completed, B=queued
After B:  A=completed, B=completed
```

Persisted AgentRun counts: coding=2, test=3, debug=1, recovery=2, review=1,
pull_request=1. Planner output is represented by the plan and workflow checkpoint,
not an additional planner AgentRun in this implementation.

| Test run | Context | Exit | Result | Duration |
| --- | --- | --- | --- | --- |
| 1 | A initial patch | 1 | Expected failure: AssertionError: 3 != 2 | 1.484 s |
| 2 | A after debug attempt 1 | 0 | PASS, one test | 1.469 s |
| 3 | B documentation change | 0 | PASS, one test | 1.390 s |

No test timed out or truncated output. Final workflow and execution status: completed.
Final review: approved=true, findings=[], mechanical_errors=[].

Branch: `ai-platform/issue-1-6f5e6224810e5b29b337c076b7466275`.
Commit: `01ec5b29428e77133295ddb12a5969e0ad8de77d`.
Exactly `README.md` and `calc.py` changed. The committed function returns 2.

## Reproduction commands

Run from the repository root with the existing virtual environment:

```powershell
docker compose up -d --wait --wait-timeout 180
$env:RUN_DATABASE_TESTS='1'
$env:RUN_WORKER_TESTS='1'
$env:RUN_SANDBOX_TESTS='1'
Get-Content .env.example | ForEach-Object {
    if ($_ -match '^DATABASE_URL=(.+)$') { $env:TEST_DATABASE_URL=$matches[1] }
    if ($_ -match '^(CELERY_BROKER_URL|CELERY_RESULT_BACKEND)=(.+)$') {
        [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
    }
}
.\.venv\Scripts\python.exe -m pytest tests/integration/test_workflow.py::test_api_redis_worker_workflow -q -s
```

This uses development placeholders from `.env.example`, not real credentials.
On a differently configured local database, use its existing local test configuration.
The live command passed: 1 test, 2 dependency deprecation warnings, 22.55 seconds.
It prints `STEP24_EVIDENCE` JSON before teardown, with new IDs on every run.

Additional executed commands:

```powershell
# With RUN_DATABASE_TESTS=1 and TEST_DATABASE_URL set, worker/sandbox flags unset:
.\.venv\Scripts\python.exe -m pytest tests/unit/test_workflow.py tests/integration/test_workflow.py -q
# In a fresh shell without opt-in integration flags:
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy
git diff --check
docker compose ps
docker ps --filter 'name=platform-sandbox-' --format '{{.Names}}'
```

Default suite: 307 passed, 147 opt-in tests skipped, 2 existing dependency warnings.
Focused workflow suite: 18 passed, 1 worker test skipped, 2 warnings in 68.62 seconds.
That worker test was run separately with real Redis and Docker and passed as recorded above.
Ruff, formatting, mypy (180 files), and whitespace checks passed. Compose services were
healthy, and no running sandbox containers remained after validation.

## Audit changes and limits

Created `tests/integration/workflow_evidence.py` and this report. Enhanced
`tests/integration/test_workflow.py` with observed scheduler states and persisted evidence
assertions. No production changes were necessary during this audit.

No manual action is required for this fixture audit. External provider quality, real GitHub
publication, multi-host execution, and separate-process worker deployment are not proved by
this test. The fixture verifies one small Python change and its dependent documentation task.

Suggested commit message: `test: verify step 24 workflow with live infrastructure and audit evidence`.
