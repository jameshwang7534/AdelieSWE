"""Create a separate commit/ref without moving HEAD; publish only a new remote ref."""

import re
from pathlib import Path
from uuid import UUID

from app.core.config import Settings
from app.integrations.git.patches import LocalPatchGit, PatchGit
from app.integrations.git.runner import GitRunner, SubprocessGitRunner
from app.pull_requests.contracts import Publication, PublicationError
from app.services.patch_format import validate_path
from app.services.workspace import WorkspaceService


class LocalPublicationGit:
    def __init__(
        self, settings: Settings, local: PatchGit | None = None, remote: GitRunner | None = None
    ) -> None:
        self.settings = settings
        self.local = local or LocalPatchGit()
        self.remote = remote or SubprocessGitRunner()

    def _run(self, workspace: Path, args: list[str]) -> str:
        result = self.local.run(args, workspace)
        if result.code:
            raise PublicationError("publication_git_failed")
        return result.stdout.strip()

    def prepare(
        self, workspace: Path, branch: str, execution_id: UUID
    ) -> tuple[str, str, list[str]]:
        # Preserve the reviewed tracked/untracked diff representation and caller staging.
        index = workspace / ".git/index"
        previous = index.read_bytes() if index.exists() else None
        try:
            return self._prepare(workspace, branch, execution_id)
        finally:
            if previous is None:
                index.unlink(missing_ok=True)
            else:
                index.write_bytes(previous)

    def _prepare(
        self, workspace: Path, branch: str, execution_id: UUID
    ) -> tuple[str, str, list[str]]:
        if not re.fullmatch(r"ai-platform/issue-[1-9][0-9]*-[0-9a-f]{32}", branch):
            raise PublicationError("invalid_publication_branch")
        ref = f"refs/heads/{branch}"
        parent = self._run(workspace, ["rev-parse", "HEAD"])
        tracked = self._run(workspace, ["diff", "--name-only", "-z", "HEAD"])
        untracked = self._run(workspace, ["ls-files", "--others", "--exclude-standard", "-z"])
        files = sorted(set(filter(None, (tracked + "\x00" + untracked).split("\x00"))))
        if not files:
            raise PublicationError("no_changes_to_publish")
        for name in files:
            validate_path(name)
        self._run(workspace, ["add", "--", *files])
        tree = self._run(workspace, ["write-tree"])
        if tree == self._run(workspace, ["rev-parse", "HEAD^{tree}"]):
            raise PublicationError("no_changes_to_publish")
        message = f"Implement execution {execution_id}"
        existing = self.local.run(["show-ref", "--verify", "--hash", ref], workspace)
        if existing.code == 0:
            commit = existing.stdout.strip()
            identity = self._run(workspace, ["show", "-s", "--format=%T%n%P%n%B", commit])
            if identity != f"{tree}\n{parent}\n{message}":
                raise PublicationError("local_branch_collision")
        else:
            # Plumbing creates a commit on the publication ref, never on the default branch.
            commit = self._run(
                workspace,
                [
                    "-c",
                    f"user.name={self.settings.git_commit_name}",
                    "-c",
                    f"user.email={self.settings.git_commit_email}",
                    "commit-tree",
                    tree,
                    "-p",
                    parent,
                    "-m",
                    message,
                ],
            )
            self._run(workspace, ["update-ref", ref, commit, "0" * len(parent)])
        return parent, commit, files

    def push(self, workspace: Path, publication: Publication) -> None:
        if publication.branch == publication.base:
            raise PublicationError("default_branch_publication_forbidden")
        if not self.settings.github_token:
            raise PublicationError("github_token_required_for_publication")
        WorkspaceService(
            self.settings.workspace_root, self.remote, github_host=self.settings.github_git_host
        )._source(publication.source)
        # Empty expected value means create ONLY if absent. No existing ref can be replaced,
        # including during a race after the API existence check. Never push to origin/config.
        self.remote.run(
            [
                "push",
                "--no-verify",
                "--porcelain",
                f"--force-with-lease=refs/heads/{publication.branch}:",
                publication.source,
                f"{publication.commit}:refs/heads/{publication.branch}",
            ],
            cwd=workspace,
            token=self.settings.github_token,
        )
