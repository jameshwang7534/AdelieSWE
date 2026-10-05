"""Explicit production resources; importing this module does not connect or publish."""

from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from app.core.config import Settings
from app.db.session import create_database_engine, create_session_factory
from app.integrations.github.client import HttpGitHubClient
from app.pull_requests.contracts import PublicationError
from app.pull_requests.git import LocalPublicationGit
from app.pull_requests.records import PublicationRecords
from app.pull_requests.service import PullRequestService


@contextmanager
def publication_resources(settings: Settings) -> Iterator[PullRequestService]:
    if not settings.github_token or not settings.github_token.get_secret_value():
        raise PublicationError("github_token_required_for_publication")
    engine = create_database_engine(settings)
    try:
        with httpx.Client(
            base_url=settings.github_api_url.rstrip("/") + "/",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": f"Bearer {settings.github_token.get_secret_value()}",
            },
            timeout=30,
            follow_redirects=False,
        ) as client:
            yield PullRequestService(
                settings,
                PublicationRecords(create_session_factory(engine)),
                LocalPublicationGit(settings),
                HttpGitHubClient(client),
            )
    finally:
        engine.dispose()
