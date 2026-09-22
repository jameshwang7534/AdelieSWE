"""GitHub REST boundary. No HTTP calls or credentials escape this adapter."""

import time
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, ValidationError


class GitHubError(Exception):
    def __init__(self, code: str, status: int, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after


class OwnerData(BaseModel):
    login: str = Field(min_length=1, max_length=255)


class RepositoryData(BaseModel):
    owner: OwnerData
    name: str = Field(min_length=1, max_length=255)
    clone_url: str
    default_branch: str = Field(min_length=1, max_length=255)


class IssueData(BaseModel):
    number: int = Field(gt=0)
    title: str
    body: str | None = None
    state: str
    html_url: str
    pull_request: dict[str, Any] | None = None


class GitHubClient(Protocol):
    def get_repository(self, owner: str, repo: str) -> RepositoryData: ...
    def get_issue(self, owner: str, repo: str, number: int) -> IssueData: ...
    def get_branch_sha(self, owner: str, repo: str, branch: str) -> str: ...
    def create_branch(self, owner: str, repo: str, branch: str, sha: str) -> dict[str, Any]: ...
    def create_pull_request(
        self, owner: str, repo: str, *, title: str, head: str, base: str, body: str
    ) -> dict[str, Any]: ...
    def get_pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]: ...


def repository_path(owner: str, repo: str) -> str:
    # Encode each component independently; callers cannot supply an arbitrary URL.
    return f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}"


class HttpGitHubClient:
    """Uses a caller-owned httpx client, with a fixed base URL and no redirects/retries."""

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            response = self.client.request(method, path, json=payload)
        except httpx.RequestError:
            raise GitHubError("github_unavailable", 503) from None
        status = response.status_code
        if status in {403, 429}:
            limited = (
                status == 429
                or response.headers.get("x-ratelimit-remaining") == "0"
                or "retry-after" in response.headers
                or "rate limit" in response.text.lower()
            )
            if limited:
                delay = 60
                retry = response.headers.get("retry-after", "")
                reset = response.headers.get("x-ratelimit-reset", "")
                if retry.isdigit():
                    delay = max(1, int(retry))
                elif reset.isdigit():
                    delay = max(1, int(reset) - int(time.time()))
                raise GitHubError("github_rate_limited", 429, delay)
        errors = {401: "github_unauthorized", 403: "github_forbidden", 404: "github_not_found"}
        if status in errors:
            raise GitHubError(errors[status], status)
        if not response.is_success:
            raise GitHubError("github_response_error", 502)
        try:
            data = response.json()
        except ValueError:
            raise GitHubError("github_invalid_response", 502) from None
        if not isinstance(data, dict):
            raise GitHubError("github_invalid_response", 502)
        return data

    def get_repository(self, owner: str, repo: str) -> RepositoryData:
        try:
            return RepositoryData.model_validate(self._request("GET", repository_path(owner, repo)))
        except ValidationError:
            raise GitHubError("github_invalid_response", 502) from None

    def get_issue(self, owner: str, repo: str, number: int) -> IssueData:
        try:
            issue = IssueData.model_validate(
                self._request("GET", f"{repository_path(owner, repo)}/issues/{number}")
            )
        except ValidationError:
            raise GitHubError("github_invalid_response", 502) from None
        if issue.number != number:
            raise GitHubError("github_invalid_response", 502)
        if issue.pull_request is not None:
            raise GitHubError("github_issue_is_pull_request", 422)
        return issue

    def get_branch_sha(self, owner: str, repo: str, branch: str) -> str:
        data = self._request(
            "GET", f"{repository_path(owner, repo)}/git/ref/heads/{quote(branch, safe='')}"
        )
        obj = data.get("object")
        if not isinstance(obj, dict) or not isinstance(obj.get("sha"), str):
            raise GitHubError("github_invalid_response", 502)
        return str(obj["sha"])

    def create_branch(self, owner: str, repo: str, branch: str, sha: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"{repository_path(owner, repo)}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": sha},
        )

    def create_pull_request(
        self, owner: str, repo: str, *, title: str, head: str, base: str, body: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"{repository_path(owner, repo)}/pulls",
            {"title": title, "head": head, "base": base, "body": body},
        )

    def get_pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return self._request("GET", f"{repository_path(owner, repo)}/pulls/{number}")
