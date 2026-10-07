"""Inspect emitted JSON during the real Redis/Postgres/Docker fixture workflow."""

import io
import json
import logging
import os
from pathlib import Path

import pytest
from sqlalchemy import Engine

from app.core.observability import JsonFormatter
from tests.integration.test_database import engine as engine
from tests.integration.test_workflow import test_api_redis_worker_workflow as run_workflow

pytestmark = pytest.mark.skipif(
    any(
        os.environ.get(key) != "1"
        for key in ("RUN_DATABASE_TESTS", "RUN_WORKER_TESTS", "RUN_SANDBOX_TESTS")
    ),
    reason="Requires PostgreSQL, Redis and Linux Docker",
)


def test_connected_workflow_logs(
    engine: Engine,
    tmp_path: Path,
    local_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinels = ("audit-github-secret", "audit-llm-secret")
    monkeypatch.setenv("GITHUB_TOKEN", sentinels[0])
    monkeypatch.setenv("LLM_API_KEY", sentinels[1])
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("app")
    logger.addHandler(handler)
    try:
        run_workflow(engine, tmp_path, local_repository, monkeypatch)
    finally:
        logger.removeHandler(handler)
    raw = stream.getvalue()
    logs = [json.loads(line) for line in raw.splitlines()]
    request = next(r for r in logs if r["event"] == "http.request" and r["method"] == "POST")
    correlation = request["correlation_id"]
    linked = [r for r in logs if r.get("correlation_id") == correlation]
    required = {"http.request", "workflow.stage", "celery.started", "docker.execute"}
    assert required <= {r["event"] for r in linked}
    docker = [r for r in linked if r["event"] == "docker.execute"]
    assert docker and all(
        all(
            r.get(key)
            for key in ("execution_id", "agent_run_id", "celery_task_id", "task_execution_id")
        )
        for r in docker
    )
    execution = docker[0]["execution_id"]
    completed = [
        r
        for r in linked
        if r["event"] == "state.transition"
        and r.get("execution_id") == execution
        and r.get("to_state") == "completed"
    ]
    assert any("duration_ms" in r for r in completed)
    failed = [r for r in docker if r["outcome"] == "failed"]
    assert failed and all(r["duration_ms"] >= 0 for r in failed)
    assert any(r["outcome"] == "ok" for r in docker)
    for secret in sentinels:
        assert secret not in raw
    assert "authorization" not in raw.lower()
    print(
        "STEP26_TRACE="
        + json.dumps(
            {
                "correlation_id": correlation,
                "execution_id": execution,
                "events": len(linked),
                "docker_attempts": len(docker),
                "failed_docker_attempts": len(failed),
                "terminal_events": len(completed),
                "stages": sorted({r["event"] for r in linked}),
                "secret_matches": 0,
                "authorization_matches": 0,
            },
            sort_keys=True,
        )
    )
