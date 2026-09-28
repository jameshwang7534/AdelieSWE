"""Fail-closed workspace/environment checks before Docker can see user code."""

import os
import re
import stat
from pathlib import Path
from uuid import UUID

from app.sandbox.base import SandboxError, SandboxRequest

SAFE_ENVIRONMENT = frozenset(
    {"CI", "LANG", "LC_ALL", "TZ", "PYTHONHASHSEED", "PYTHONDONTWRITEBYTECODE"}
)


def _walk_error(error: OSError) -> None:
    raise error


def workspace_path(root: Path, workspace: Path) -> Path:
    try:
        root = root.absolute()
        workspace = workspace.absolute()
        if root == Path(root.anchor) or workspace == root or not workspace.is_relative_to(root):
            raise SandboxError("unsafe_workspace")
        for part in (workspace, *workspace.parents):
            if part.is_symlink() or part.is_junction():
                raise SandboxError("unsafe_workspace")
        resolved = workspace.resolve(strict=True)
        if (
            not resolved.is_dir()
            or resolved == root.resolve(strict=True)
            or not resolved.is_relative_to(root.resolve(strict=True))
        ):
            raise SandboxError("unsafe_workspace")
        relative = resolved.relative_to(root.resolve(strict=True)).parts
        if (
            len(relative) != 3
            or relative[0] != "executions"
            or any(str(UUID(part)) != part for part in relative[1:])
        ):
            raise SandboxError("unsafe_workspace")
        if any(character in str(resolved) for character in ",\n\r\x00"):
            raise SandboxError("unsafe_workspace")
        # No linked/special files or well-known secret files in a task bind mount.
        for directory, folders, files in os.walk(resolved, followlinks=False, onerror=_walk_error):
            for name in folders + files:
                path = Path(directory) / name
                lowered = name.lower()
                if (
                    lowered.startswith(".env")
                    or lowered
                    in {
                        ".ssh",
                        ".aws",
                        ".kube",
                        "credentials",
                        "credentials.json",
                        "secrets.json",
                        "secrets.yaml",
                        ".netrc",
                    }
                    or lowered.endswith((".pem", ".key", ".p12", ".pfx"))
                ):
                    raise SandboxError("workspace_contains_sensitive_files")
                if path.is_symlink() or path.is_junction() or path.is_mount():
                    raise SandboxError("unsafe_workspace")
                info = path.lstat()
                if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    raise SandboxError("unsafe_workspace")
                if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                    raise SandboxError("unsafe_workspace")
        return resolved
    except (OSError, ValueError):
        raise SandboxError("unsafe_workspace") from None


def environment_values(request: SandboxRequest) -> list[str]:
    if not request.environment_allowlist <= SAFE_ENVIRONMENT:
        raise SandboxError("environment_not_allowed")
    if not set(request.environment) <= request.environment_allowlist:
        raise SandboxError("environment_not_allowed")
    values = ["PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/tmp", "TMPDIR=/tmp"]
    for key, value in sorted(request.environment.items()):
        if len(value) > 128 or not re.fullmatch(r"[A-Za-z0-9_.:/+ -]*", value):
            raise SandboxError("environment_not_allowed")
        values.append(f"{key}={value}")
    return values
