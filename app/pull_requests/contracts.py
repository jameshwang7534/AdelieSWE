"""Publication boundaries and persisted, credential-free operation data."""

from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, Field


class PublicationError(Exception):
    """Fixed error codes only; remote responses and Git stderr are never included."""


class Publication(BaseModel):
    execution_id: UUID
    review_id: UUID
    owner: str
    repository: str
    source: str
    base: str
    branch: str
    parent: str
    commit: str
    diff_hash: str
    title: str
    body: str


class PublishedPR(BaseModel):
    number: int = Field(gt=0, strict=True)
    html_url: str
    state: str


class PublicationGit(Protocol):
    def prepare(
        self, workspace: Path, branch: str, execution_id: UUID
    ) -> tuple[str, str, list[str]]:
        """Return baseline SHA, publication commit SHA, changed paths; preserve HEAD."""
        ...

    def push(self, workspace: Path, publication: Publication) -> None: ...


class PublicationGitHub(Protocol):
    def get_branch_sha(self, owner: str, repo: str, branch: str) -> str: ...
    def find_pull_requests(
        self, owner: str, repo: str, *, head: str, base: str
    ) -> list[dict[str, Any]]: ...
    def create_pull_request(
        self, owner: str, repo: str, *, title: str, head: str, base: str, body: str
    ) -> dict[str, Any]: ...
