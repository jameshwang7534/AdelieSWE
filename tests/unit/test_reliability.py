"""Failure classification and transition policy without external services."""

import pytest

from app.integrations.github.client import GitHubError
from app.integrations.llm.provider import LLMError
from app.models import AgentRun, TaskExecution, WorkflowRun
from app.orchestration.failures import provider_failure
from app.orchestration.state import InvalidTransition
from app.orchestration.transitions import agent_state, task_state, workflow_state


@pytest.mark.parametrize(
    "error,code,retry",
    [
        (LLMError("llm_timeout"), "llm_timeout", True),
        (LLMError("llm_invalid_response"), "llm_invalid_response", False),
        (GitHubError("github_unavailable", 503), "github_unavailable", True),
        (GitHubError("github_rate_limited", 429, 120), "github_rate_limited", True),
        (GitHubError("github_rate_limited", 429, 4000), "github_rate_limited", False),
        (GitHubError("github_not_found", 404), "github_not_found", False),
        (GitHubError("private-response", 401), "github_request_failed", False),
        (TimeoutError("private"), "workflow_timeout", True),
        (RuntimeError("private"), "workflow_stage_failed", False),
    ],
)
def test_safe_failure_codes(error: Exception, code: str, retry: bool) -> None:
    result = provider_failure(error)
    assert result[:2] == (code, retry)
    assert "private" not in str(result)


def test_terminals_cannot_be_resurrected() -> None:
    task = TaskExecution(status="completed")
    task_state(task, "completed")
    with pytest.raises(InvalidTransition):
        task_state(task, "running")
    with pytest.raises(InvalidTransition):
        task_state(task, "invented")
    with pytest.raises(InvalidTransition):
        workflow_state(WorkflowRun(status="cancelled"), "pending")
    with pytest.raises(InvalidTransition):
        agent_state(AgentRun(status="failed", agent_type="coding"), "completed")
    journal = AgentRun(status="failed", agent_type="pull_request")
    agent_state(journal, "completed")
    assert journal.status == "completed"
