"""Explicit, retryable publication; no automatic dispatch or real GitHub calls in tests."""

import re
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from app.core.config import Settings
from app.integrations.github.client import GitHubError
from app.pull_requests.body import render_body
from app.pull_requests.contracts import (
    Publication,
    PublicationError,
    PublicationGit,
    PublicationGitHub,
    PublishedPR,
)
from app.pull_requests.records import PublicationRecords
from app.schemas.context import IssueContext
from app.schemas.testing import RepositoryTestConfig
from app.services.code_patches import CodePatchService


class PullRequestService:
    def __init__(
        self,
        settings: Settings,
        records: PublicationRecords,
        git: PublicationGit,
        github: PublicationGitHub,
    ) -> None:
        self.settings, self.records, self.git, self.github = settings, records, git, github
        self.patches = CodePatchService(settings.workspace_root)

    def _remote_pr(self, data: dict[str, Any], publication: Publication) -> PublishedPR:
        marker = f"<!-- ai-platform-execution:{publication.execution_id} -->"
        expected_repo = f"{publication.owner}/{publication.repository}"
        head, base = data.get("head", {}), data.get("base", {})
        if (
            not isinstance(head, dict)
            or not isinstance(base, dict)
            or head.get("ref") != publication.branch
            or head.get("sha") != publication.commit
            or base.get("ref") != publication.base
            or head.get("repo", {}).get("full_name", "").casefold() != expected_repo.casefold()
            or base.get("repo", {}).get("full_name", "").casefold() != expected_repo.casefold()
            or marker not in str(data.get("body", ""))
        ):
            raise PublicationError("publication_pr_collision")
        result = PublishedPR.model_validate(data)
        expected_url = (
            f"https://{self.settings.github_git_host}/{expected_repo}/pull/{result.number}"
        )
        if result.html_url != expected_url or result.state not in {"open", "closed"}:
            raise PublicationError("publication_invalid_pr")
        return result

    def run(
        self, execution_id: UUID, context: IssueContext, required: RepositoryTestConfig
    ) -> PublishedPR:
        if any(
            name not in self.settings.test_allowed_commands for name in required.required_commands
        ):
            raise PublicationError("publication_required_command_disabled")
        workspace = (
            self.settings.workspace_root
            / "executions"
            / str(context.repository.id)
            / str(execution_id)
        )
        with self.patches.locked(workspace) as safe:
            evidence = self.records.check(execution_id, context, required)
            diff = self.patches.current_diff(safe)
            digest = sha256(diff.encode()).hexdigest()
            if not diff or digest != evidence.decision.diff_hash:
                raise PublicationError("publication_workspace_changed")
            secrets = (
                self.settings.github_token,
                self.settings.llm_api_key,
                self.settings.database_url,
                self.settings.redis_url,
            )
            if any(
                secret and secret.get_secret_value() and secret.get_secret_value() in diff
                for secret in secrets
            ):
                raise PublicationError("publication_contains_secret")
            branch = f"ai-platform/issue-{evidence.issue_number}-{execution_id.hex}"
            if not all(
                re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", value)
                for value in (evidence.owner, evidence.repository)
            ):
                raise PublicationError("publication_invalid_repository")
            if branch == evidence.base:
                raise PublicationError("default_branch_publication_forbidden")
            # Source is derived from the persisted repository identity, never a Git remote.
            expected = f"https://{self.settings.github_git_host}/{evidence.owner}/{evidence.repository}.git"
            if evidence.source != expected or urlsplit(expected).username is not None:
                raise PublicationError("publication_untrusted_repository_url")
            parent, commit, files = self.git.prepare(safe, branch, execution_id)
            publication = Publication(
                execution_id=execution_id,
                review_id=evidence.review_id,
                owner=evidence.owner,
                repository=evidence.repository,
                source=evidence.source,
                base=evidence.base,
                branch=branch,
                parent=parent,
                commit=commit,
                diff_hash=digest,
                title=f"Resolve #{evidence.issue_number}: {evidence.title}"[:250],
                body=render_body(evidence, execution_id, files),
            )
            for secret in secrets:
                if (
                    secret
                    and secret.get_secret_value()
                    and secret.get_secret_value() in publication.model_dump_json()
                ):
                    raise PublicationError("publication_contains_secret")
            if self.patches.current_diff(safe) != diff:
                raise PublicationError("publication_workspace_changed")
            self.records.check(execution_id, context, required)
            journal_id = self.records.journal(publication)
            try:
                try:
                    remote_sha = self.github.get_branch_sha(
                        evidence.owner, evidence.repository, branch
                    )
                except GitHubError as error:
                    if error.status != 404:
                        raise
                    remote_sha = None
                if remote_sha is not None and remote_sha != commit:
                    raise PublicationError("remote_branch_collision")
                if remote_sha is None:
                    self.git.push(safe, publication)
                existing = self.github.find_pull_requests(
                    evidence.owner,
                    evidence.repository,
                    head=branch,
                    base=evidence.base,
                )
                if len(existing) > 1:
                    raise PublicationError("publication_pr_collision")
                if existing:
                    data = existing[0]
                else:
                    data = self.github.create_pull_request(
                        evidence.owner,
                        evidence.repository,
                        title=publication.title,
                        head=branch,
                        base=evidence.base,
                        body=publication.body,
                    )
                remote = self._remote_pr(data, publication)
                return self.records.finish(publication, journal_id, remote)
            except Exception as error:
                code = (
                    str(error)
                    if isinstance(error, PublicationError)
                    else "publication_retry_required"
                )
                # If the DB itself is unavailable the committed journal remains sufficient to retry.
                try:
                    self.records.failed(journal_id, code)
                except Exception:
                    raise PublicationError(
                        "publication_journal_unavailable_retry_required"
                    ) from None
                raise PublicationError(code) from None
