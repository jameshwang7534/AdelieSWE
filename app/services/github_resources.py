"""Lifespan-owned database engine and HTTP connection pool; both connect lazily."""

from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from app.core.config import Settings
from app.db.session import create_database_engine, create_session_factory
from app.integrations.github.client import HttpGitHubClient
from app.services.repositories import RepositoryService


@contextmanager
def repository_resources(settings: Settings) -> Iterator[RepositoryService | None]:
    if settings.database_url is None:
        yield None
        return
    engine = create_database_engine(settings)
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if settings.github_token is not None:
        headers["Authorization"] = f"Bearer {settings.github_token.get_secret_value()}"
    try:
        with httpx.Client(
            base_url=settings.github_api_url.rstrip("/") + "/",
            headers=headers,
            timeout=settings.dependency_timeout_seconds,
            follow_redirects=False,
        ) as client:
            yield RepositoryService(create_session_factory(engine), HttpGitHubClient(client))
    finally:
        engine.dispose()
