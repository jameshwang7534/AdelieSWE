"""GitHub transport contracts and error behavior with no real GitHub requests."""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.integrations.github.client import GitHubError, HttpGitHubClient
from app.main import create_app
from app.services.github_resources import repository_resources
from app.services.repositories import RecordNotFound, RepositoryService


@pytest.mark.parametrize(
    ("status", "headers", "message", "code", "expected"),
    [
        (401, {}, "secret", "github_unauthorized", 401),
        (403, {}, "secret", "github_forbidden", 403),
        (404, {}, "secret", "github_not_found", 404),
        (403, {"x-ratelimit-remaining": "0"}, "secret", "github_rate_limited", 429),
        (403, {}, "secondary rate limit exceeded", "github_rate_limited", 429),
        (429, {"retry-after": "12"}, "secret", "github_rate_limited", 429),
        (500, {}, "secret", "github_response_error", 502),
        (302, {"location": "https://untrusted.invalid"}, "secret", "github_response_error", 502),
    ],
)
def test_http_errors(
    status: int,
    headers: dict[str, str],
    message: str,
    code: str,
    expected: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, json={"message": message})

    with httpx.Client(
        base_url="https://github.invalid/", transport=httpx.MockTransport(handler)
    ) as c:
        with pytest.raises(GitHubError) as error:
            HttpGitHubClient(c).get_repository("owner", "repo")
        assert error.value.code == code and error.value.status == expected
        assert "secret" not in str(error.value)
        if status == 429:
            assert error.value.retry_after == 12


def test_network_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret", request=request)

    with httpx.Client(
        base_url="https://github.invalid/", transport=httpx.MockTransport(handler)
    ) as c:
        with pytest.raises(GitHubError, match="github_unavailable"):
            HttpGitHubClient(c).get_repository("owner", "repo")


@pytest.mark.parametrize("payload", [[], {}, {"number": 2}, "not json"])
def test_invalid_payload(payload: object) -> None:
    with httpx.Client(
        base_url="https://github.invalid/",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)),
    ) as c:
        with pytest.raises(GitHubError, match="github_invalid_response"):
            HttpGitHubClient(c).get_repository("owner", "repo")


def test_branch_and_pr_contracts() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"object": {"sha": "abc"}, "number": 1})

    with httpx.Client(
        base_url="https://github.invalid/api/v3/",
        transport=httpx.MockTransport(handler),
    ) as c:
        client = HttpGitHubClient(c)
        assert client.get_branch_sha("owner", "repo", "feature/test") == "abc"
        client.create_branch("owner", "repo", "feature/test", "abc")
        client.create_pull_request(
            "owner", "repo", title="T", head="feature/test", base="main", body="B"
        )
        assert client.get_pull_request("owner", "repo", 1)["number"] == 1
    assert requests[0].url.raw_path.endswith(b"/git/ref/heads/feature%2Ftest")
    assert requests[1].method == "POST" and b"refs/heads/feature/test" in requests[1].content
    assert requests[2].url.path.endswith("/pulls") and b'"base":"main"' in requests[2].content
    assert requests[3].method == "GET"


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (GitHubError("github_unauthorized", 401), 401, "github_unauthorized"),
        (GitHubError("github_forbidden", 403), 403, "github_forbidden"),
        (GitHubError("github_not_found", 404), 404, "github_not_found"),
        (GitHubError("github_rate_limited", 429, 60), 429, "github_rate_limited"),
        (GitHubError("github_unavailable", 503), 503, "github_unavailable"),
        (RecordNotFound(), 404, "record_not_found"),
        (OperationalError("secret", {}, Exception("secret")), 503, "database_unavailable"),
    ],
)
def test_api_errors(error: Exception, status: int, code: str) -> None:
    service = Mock(spec=RepositoryService)
    service.register.side_effect = error

    @contextmanager
    def resources(settings: Settings) -> Iterator[RepositoryService | None]:
        yield service

    settings = Settings(database_url=None, redis_url=None, celery_broker_url=None)
    with TestClient(create_app(settings, repository_factory=resources)) as api:
        response = api.post("/repositories", json={"github_owner": "owner", "github_name": "repo"})
        assert response.status_code == status
        assert response.json() == {"detail": {"code": code}}
        if status == 429:
            assert response.headers["retry-after"] == "60"
        assert api.get("/health").status_code == 200
        assert (
            api.post(
                "/repositories", json={"github_owner": "../x", "github_name": "repo"}
            ).status_code
            == 422
        )
        assert api.post(f"/repositories/{uuid4()}/issues/0/import").status_code == 422


def test_unconfigured_database() -> None:
    with TestClient(
        create_app(Settings(database_url=None, redis_url=None, celery_broker_url=None))
    ) as c:
        assert c.get(f"/issues/{uuid4()}").status_code == 503


def test_pull_request_is_not_imported_as_issue() -> None:
    payload = {
        "number": 1,
        "title": "PR",
        "state": "open",
        "html_url": "https://github.invalid/1",
        "pull_request": {},
    }
    with httpx.Client(
        base_url="https://github.invalid/",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)),
    ) as c:
        with pytest.raises(GitHubError, match="github_issue_is_pull_request"):
            HttpGitHubClient(c).get_issue("owner", "repo", 1)


@pytest.mark.parametrize("token", [None, SecretStr("synthetic-test-token")])
def test_resource_headers_and_cleanup(token: SecretStr | None) -> None:
    settings = Settings(
        database_url=SecretStr("postgresql+psycopg://localhost/test"),
        github_token=token,
        github_api_url="https://github.invalid/api/v3",
    )
    engine = Mock()
    with (
        patch("app.services.github_resources.create_database_engine", return_value=engine),
        patch("httpx.Client", autospec=True) as client,
    ):
        with repository_resources(settings) as service:
            assert service is not None
            options = client.call_args.kwargs
            assert options["base_url"] == "https://github.invalid/api/v3/"
            assert options["follow_redirects"] is False
            assert options["headers"]["Accept"] == "application/vnd.github+json"
            if token is None:
                assert "Authorization" not in options["headers"]
            else:
                assert options["headers"]["Authorization"] == "Bearer synthetic-test-token"
        engine.dispose.assert_called_once()
        client.return_value.__exit__.assert_called_once()


@pytest.mark.parametrize(
    "url",
    [
        "http://github.invalid",
        "https://user:pass@github.invalid",
        "https://github.invalid/?token=x",
    ],
)
def test_reject_unsafe_base_url(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(github_api_url=url)
