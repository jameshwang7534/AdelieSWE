"""Validate every file before atomic git apply; no commits, shell commands, or tests."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.integrations.git.patches import LocalPatchGit, PatchGit
from app.sandbox.policy import workspace_path
from app.schemas.coding import CodeChangeProposal
from app.services.patch_format import PatchError, parse_patch


class CodePatchService:
    def __init__(self, root: Path, git: PatchGit | None = None) -> None:
        self.root, self.git = root, git or LocalPatchGit()

    @contextmanager
    def locked(self, workspace: Path) -> Iterator[Path]:
        # Same managed layout as sandbox. Reject links/secrets before reading repository content.
        safe = workspace_path(self.root, workspace)
        lock = safe.parent / f".{safe.name}.coding.lock"
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise PatchError("coding_workspace_busy") from None
        try:
            os.close(descriptor)
            self.verify(safe)
            yield safe
        finally:
            lock.unlink()

    def verify(self, workspace: Path) -> None:
        if not (workspace / ".git").is_dir():
            raise PatchError("standalone_git_repository_required")
        keys = self.git.run(
            ["config", "--local", "--no-includes", "--name-only", "--list"], workspace
        )
        permitted = {
            "core.repositoryformatversion",
            "core.filemode",
            "core.bare",
            "core.logallrefupdates",
            "core.symlinks",
            "core.ignorecase",
            "user.name",
            "user.email",
        }
        if keys.code or any(key.lower() not in permitted for key in keys.stdout.splitlines()):
            raise PatchError("unsafe_local_git_configuration")
        top = self.git.run(["rev-parse", "--show-toplevel"], workspace)
        if top.code or Path(top.stdout.strip()).resolve() != workspace.resolve():
            raise PatchError("invalid_git_workspace")
        if (workspace / ".git/objects/info/alternates").exists():
            raise PatchError("unsafe_git_alternates")

    def status(self, workspace: Path) -> str:
        result = self.git.run(["status", "--porcelain", "--untracked-files=all"], workspace)
        if result.code:
            raise PatchError("patch_status_failed", result.stderr)
        return "clean" if not result.stdout.strip() else "modified"

    def apply(self, workspace: Path, proposal: CodeChangeProposal) -> str:
        validated = CodeChangeProposal.model_validate(proposal.model_dump())
        files = parse_patch(validated.unified_diff, validated.files_changed)
        workspace_path(self.root, workspace)
        self.verify(workspace)
        for item in files:
            target = workspace / item.path
            if item.new and target.exists():
                raise PatchError("new_file_already_exists")
            if not item.new and not target.is_file():
                raise PatchError("patch_file_missing")
            if target.exists() and target.stat().st_size > 1048576:
                raise PatchError("patch_file_too_large")
            if not target.resolve().is_relative_to(workspace.resolve()):
                raise PatchError("unsafe_patch_path")
        checked = self.git.run(
            ["apply", "--check", "--whitespace=nowarn", "-"], workspace, validated.unified_diff
        )
        if checked.code:
            raise PatchError("patch_check_failed", checked.stderr)
        applied = self.git.run(
            ["apply", "--whitespace=nowarn", "-"], workspace, validated.unified_diff
        )
        if applied.code:
            raise PatchError("patch_apply_failed", applied.stderr)
        diff = self.git.run(
            ["diff", "--no-ext-diff", "--no-textconv", "HEAD", "--", *validated.files_changed],
            workspace,
        )
        if diff.code:
            raise PatchError("patch_diff_failed", diff.stderr)
        output = diff.stdout
        for item in files:
            tracked = self.git.run(["ls-files", "--", item.path], workspace)
            if tracked.code:
                raise PatchError("patch_diff_failed", tracked.stderr)
            if not tracked.stdout.strip() and (workspace / item.path).is_file():
                addition = self.git.run(
                    [
                        "diff",
                        "--no-ext-diff",
                        "--no-textconv",
                        "--no-index",
                        "--",
                        os.devnull,
                        item.path,
                    ],
                    workspace,
                )
                if addition.code not in {0, 1}:
                    raise PatchError("patch_diff_failed", addition.stderr)
                output += addition.stdout
        return output
