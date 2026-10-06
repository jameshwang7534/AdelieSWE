"""Conservative stage retry classification; ambiguous writes require reconciliation."""

from app.integrations.github.client import GitHubError
from app.integrations.llm.provider import LLMError

SAFE_RETRY_STAGES = {"repository", "issue", "context", "plan"}


def provider_failure(error: Exception) -> tuple[str, bool, int]:
    if isinstance(error, LLMError):
        return error.code, error.code in {"llm_timeout", "llm_unavailable", "llm_rate_limited"}, 0
    if isinstance(error, GitHubError):
        codes = {
            "github_unavailable",
            "github_rate_limited",
            "github_response_error",
            "github_authentication_failed",
            "github_unauthorized",
            "github_forbidden",
            "github_not_found",
            "github_invalid_response",
            "github_issue_is_pull_request",
        }
        code = error.code if error.code in codes else "github_request_failed"
        delay = max(0, error.retry_after or 0)
        return (
            code,
            code in {"github_unavailable", "github_rate_limited", "github_response_error"}
            and error.status in {429, 502, 503, 504}
            and delay <= 3600,
            delay,
        )
    if isinstance(error, TimeoutError):
        return "workflow_timeout", True, 0
    return "workflow_stage_failed", False, 0
