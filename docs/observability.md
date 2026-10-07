# Observability (Step 26)

The API and Celery workers emit newline-delimited JSON through Python logging.
No hosted service, collector, new secret, or additional dependency is required.
`LOG_LEVEL` controls application verbosity. Framework handlers are formatted at API
startup and Celery logging setup; repeated setup does not add duplicate handlers.

Each entry includes UTC timestamp, level, logger and event. UUID identifiers are included
when present in the active operation: request_id, correlation_id, workflow_id,
repository_id, issue_id, plan_id, execution_id, plan_task_id, task_execution_id,
agent_run_id and celery_task_id. Unknown IDs are omitted rather than invented.

FastAPI accepts UUID `X-Request-ID` and `X-Correlation-ID` headers. Invalid values are
replaced by generated UUIDs. Both IDs are returned in response headers, including
unhandled 500 responses. Request events use the route template rather than the raw URL;
query strings, bodies and incoming headers are never serialized. Generic 500 responses
contain an internal_error code rather than exception details.

Context is local to each asynchronous request/task and restored on exit. Celery messages
carry validated IDs in a platform_context header; workers bind and clear it per delivery.
The workflow correlation ID is saved with its existing JSON checkpoint and returned by
GET /workflows/{id}, allowing broker recovery to retain correlation. Existing workflow
rows without this field use their workflow ID on the next dispatch.

## Timing and status

Durations use monotonic clocks and milliseconds for operations:

* indexing.source, indexing.embeddings
* retrieval.bm25, retrieval.vector, retrieval.hybrid
* llm.generate, llm.embeddings (includes provider retries/backoff)
* docker.execute, tests.run
* agent.coding, agent.recovery, agent.review
* workflow.stage, workflow.delivery, repository.delivery
* celery.started/celery.finished and http.request

Operation outcomes distinguish ok, failed, timeout, and raised errors. Timings for a
parent operation include nested operations; do not sum parent and child durations.
Provider prompts, completions, source snippets, patches, test output and shell commands
are excluded. Existing stored test output remains available through the platform's
persisted records; it is not copied into logs.

state.transition is emitted only after the owning session commits, with from_state,
to_state and applicable IDs. Transaction rollbacks emit no successful transition event.
Terminal execution transitions include duration_ms measured from persisted started_at;
this elapsed wall time includes waiting between stages and survives worker restarts.
Transitions that do not change state do not emit duplicate transition events.

GET /executions/{id} adds elapsed_seconds and task_counts alongside its existing task
IDs, dependency states, attempts, timestamps and failure summaries. Completed execution
elapsed time is stable; active elapsed time increases. Pending runs return null elapsed
time. Execution completion and workflow review/publication completion remain separate.

## Secret handling and limits

Only approved metadata fields are serialized. Unknown extras such as environment,
headers, prompt or response payload are dropped. Configured SecretStr values (including
GitHub/LLM credentials and connection URLs) are redacted; authorization assignments,
secret assignments and URLs are scrubbed from application messages. Exceptions record
their type, never their text or traceback. Third-party message bodies are replaced with
external_log while retaining severity and logger identity: Celery result messages and
HTTP debug logs can otherwise expose complete inputs or results.

This intentionally trades third-party free-text diagnostics for privacy. Do not add
unmanaged handlers that bypass the JSON formatter or intentionally log sensitive data.
Log retention/rotation belongs to the host/container runtime; no log database, metrics
server, tracing exporter, or hosted observability integration is introduced.

## Validation

Unit tests cover JSON fields, redaction, excluded payloads/tracebacks, HTTP correlation
and 500 responses, concurrent async context isolation, Celery header propagation/reset,
and elapsed execution status. PostgreSQL tests prove rollback events are absent and
terminal duration/counts survive reload. The existing live workflow exercises indexing,
retrieval, coding/debugging/review, real Docker tests and Redis/Celery with this logging.

Use the existing development environment:

```powershell
python -m pytest tests/unit/test_observability.py -q
# RUN_DATABASE_TESTS=1 and existing TEST_DATABASE_URL:
python -m pytest tests/integration/test_observability.py -q
python -m ruff check .
python -m ruff format --check .
python -m mypy
```

No database migration or new external setup is required.
