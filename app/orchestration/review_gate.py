"""Mechanical test evidence checks; no model output participates in this decision."""

import json

from pydantic import ValidationError

from app.agents.testing import COMMANDS
from app.models import AgentRun, TaskExecution
from app.schemas.review import ReviewTestEvidence
from app.schemas.testing import RepositoryTestConfig, TestReport


def test_evidence(
    tasks: dict[str, TaskExecution], agents: list[AgentRun], required: RepositoryTestConfig
) -> tuple[list[ReviewTestEvidence], list[str], str | None]:
    evidence: list[ReviewTestEvidence] = []
    errors: list[str] = []
    expected = {COMMANDS[name][1] for name in required.required_commands if name in COMMANDS}
    if any(name not in COMMANDS for name in required.required_commands) or not expected:
        return [], ["invalid_required_test_policy"], None
    latest_tests: list[AgentRun] = []
    for key, task in tasks.items():
        if task.status != "completed":
            errors.append(f"task_incomplete:{key}")
        history = [a for a in agents if a.task_execution_id == task.id]
        checks = [a for a in history if a.agent_type == "test"]
        patches = [
            a for a in history if a.agent_type in {"coding", "debug"} and a.status == "completed"
        ]
        if not checks or not patches:
            errors.append(f"missing_test_evidence:{key}")
            continue
        check, patch = checks[-1], patches[-1]
        latest_tests.append(check)
        try:
            report = TestReport.model_validate_json(
                json.dumps(check.output_metadata.get("report")), strict=True
            )
        except ValidationError:
            errors.append(f"invalid_test_report:{key}")
            continue
        evidence.append(ReviewTestEvidence(task_key=key, agent_run_id=check.id, report=report))
        if (
            check.status != "completed"
            or check.output_metadata.get("error_code") is not None
            or not report.passed
            or not report.results
            or any(r.exit_code != 0 or r.timeout or not r.passed for r in report.results)
        ):
            errors.append(f"required_tests_failed:{key}")
        actual = [r.command for r in report.results]
        if set(actual) != expected or len(actual) != len(expected):
            errors.append(f"required_tests_missing:{key}")
        if (
            check.input_metadata.get("coding_agent_run_id") != str(patch.id)
            or check.started_at is None
            or patch.completed_at is None
            or check.started_at < patch.completed_at
        ):
            errors.append(f"stale_task_tests:{key}")
    latest = latest_tests[-1] if latest_tests else None
    # The list of tasks is DAG ordered, so choose the actual latest test by agent order instead.
    for agent in agents:
        if agent in latest_tests:
            latest = agent
    fingerprint = latest.output_metadata.get("workspace_diff_hash") if latest else None
    if not isinstance(fingerprint, str):
        errors.append("missing_workspace_test_fingerprint")
        fingerprint = None
    if any(a.status == "running" for a in agents):
        errors.append("implementation_agent_running")
    return evidence, errors, fingerprint
