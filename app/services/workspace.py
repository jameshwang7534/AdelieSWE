"""Managed disposable checkouts with UUID paths and per-repository filesystem locks."""

import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import SecretStr

from app.integrations.git.runner import GitRunner, WorkspaceError


@dataclass(frozen=True)
class WorkspaceResult:
    path: Path
    commit: str


class WorkspaceService:
    def __init__(
        self,
        root: Path,
        git: GitRunner,
        *,
        github_host: str = "github.com",
        token: SecretStr | None = None,
        allow_local: bool = False,
    ) -> None:
        self.root = root.absolute()
        self.git = git
        self.github_host = github_host
        self.token = token
        self.allow_local = allow_local  # Explicit dependency-injection option for local Git tests.

    def _safe(self, path: Path) -> Path:
        if not path.is_relative_to(self.root):
            raise WorkspaceError("unsafe_workspace_path")
        for part in [path, *path.parents]:
            if part.is_symlink() or part.is_junction():
                raise WorkspaceError("unsafe_workspace_path")
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise WorkspaceError("unsafe_workspace_path")
        return path

    def repository_path(self, repository_id: UUID) -> Path:
        return self._safe(self.root / "repositories" / str(UUID(str(repository_id))))

    def _initialize(self) -> None:
        self._safe(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        ignore = self._safe(self.root / ".gitignore")
        if not ignore.exists():
            try:
                with ignore.open("x", encoding="utf-8") as stream:
                    stream.write("*\n!.gitignore\n")
            except FileExistsError:
                self._safe(ignore)

    @contextmanager
    def _lock(self, repository_id: UUID) -> Iterator[None]:
        self._initialize()
        directory = self._safe(self.root / "locks")
        directory.mkdir(exist_ok=True)
        path = self._safe(directory / f"{UUID(str(repository_id))}.lock")
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise WorkspaceError("workspace_busy") from None
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(str(os.getpid()))
            yield
        finally:
            self._safe(path).unlink()

    def _source(self, source: str) -> bool:
        parsed = urlsplit(source)
        if self.allow_local and Path(source).is_absolute() and Path(source).is_dir():
            return True
        if (
            parsed.scheme != "https"
            or parsed.hostname != self.github_host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.query
            or parsed.fragment
            or any(char.isspace() for char in source)
        ):
            raise WorkspaceError("untrusted_clone_url")
        return False

    def verify(self, path: Path) -> str:
        self._safe(path)
        metadata = self._safe(path / ".git")
        if not metadata.is_dir():
            raise WorkspaceError("invalid_workspace_repository")
        # Reject linked worktrees, redirected object stores, and redirected Git metadata.
        for entry in metadata.rglob("*"):
            self._safe(entry)
        if (metadata / "objects/info/alternates").exists():
            raise WorkspaceError("shared_git_objects")
        top = self.git.run(["rev-parse", "--show-toplevel"], cwd=path)
        if Path(top).resolve() != path.resolve():
            raise WorkspaceError("invalid_workspace_repository")
        self.git.run(["fsck", "--full", "--no-reflogs"], cwd=path)
        return self.git.run(["rev-parse", "--verify", "HEAD^{commit}"], cwd=path)

    def _reset(self, path: Path, revision: str) -> str:
        self.verify(path)
        # Validate the exact target before destructive cleanup. Only managed checkouts reach here.
        commit = self.git.run(["rev-parse", "--verify", revision + "^{commit}"], cwd=path)
        self._safe(path)
        for entry in path.rglob("*"):
            self._safe(entry)
        self.git.run(["reset", "--hard", commit], cwd=path)
        self.git.run(["clean", "-ffdx"], cwd=path)
        if self.git.run(["status", "--porcelain"], cwd=path):
            raise WorkspaceError("workspace_not_clean")
        return self.verify(path)

    def reset(self, repository_id: UUID, commit: str) -> WorkspaceResult:
        """Discard managed checkout changes and reset to an existing full commit SHA."""
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            raise WorkspaceError("invalid_commit")
        with self._lock(repository_id):
            path = self.repository_path(repository_id)
            return WorkspaceResult(path, self._reset(path, commit))

    def prepare(
        self,
        repository_id: UUID,
        source: str,
        default_branch: str,
        on_status: Callable[[str], None] | None = None,
    ) -> WorkspaceResult:
        with self._lock(repository_id):
            try:
                if on_status:
                    on_status("syncing")
                local = self._source(source)
                self.git.run(["check-ref-format", f"refs/heads/{default_branch}"], cwd=self.root)
                if default_branch.startswith("-"):
                    raise WorkspaceError("invalid_default_branch")
                target = self.repository_path(repository_id)
                target.parent.mkdir(exist_ok=True)
                if target.exists():
                    self.verify(target)
                    self.git.run(["remote", "set-url", "origin", source], cwd=target)
                    self.git.run(
                        [
                            "fetch",
                            "--prune",
                            "--no-tags",
                            "origin",
                            "+refs/heads/*:refs/remotes/origin/*",
                        ],
                        cwd=target,
                        token=None if local else self.token,
                        local=local,
                    )
                else:
                    # Failed clones remain separate from the canonical repository for inspection.
                    staging = self._safe(target.parent / f".partial-{uuid4().hex}")
                    self.git.run(
                        [
                            "clone",
                            "--no-local",
                            "--no-checkout",
                            "--template=",
                            "--",
                            source,
                            str(staging),
                        ],
                        cwd=self.root,
                        token=None if local else self.token,
                        local=local,
                    )
                    self.verify(staging)
                    self._safe(staging).rename(self._safe(target))
                revision = f"refs/remotes/origin/{default_branch}"
                self._reset(target, revision)
                self.git.run(["checkout", "-B", default_branch, revision, "--"], cwd=target)
                result = WorkspaceResult(target, self.verify(target))
                if on_status:
                    on_status("ready")
                return result
            except Exception:
                if on_status:
                    on_status("error")
                raise

    def create_execution(self, repository_id: UUID, execution_id: UUID) -> WorkspaceResult:
        with self._lock(repository_id):
            source = self.repository_path(repository_id)
            commit = self.verify(source)
            target = self._safe(
                self.root
                / "executions"
                / str(UUID(str(repository_id)))
                / str(UUID(str(execution_id)))
            )
            if target.exists():
                raise WorkspaceError("execution_workspace_exists")
            target.parent.mkdir(parents=True, exist_ok=True)
            self.git.run(
                [
                    "clone",
                    "--no-local",
                    "--no-checkout",
                    "--template=",
                    "--",
                    str(source),
                    str(target),
                ],
                cwd=self.root,
                local=True,
            )
            self.git.run(["remote", "remove", "origin"], cwd=target)
            self.git.run(["checkout", "--detach", commit, "--"], cwd=target)
            return WorkspaceResult(target, self._reset(target, commit))
