"""Local Git commands for patches, with retained bounded diagnostics and no shell."""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.services.patch_format import PatchError


@dataclass(frozen=True)
class GitPatchResult:
    code: int
    stdout: str
    stderr: str


class PatchGit(Protocol):
    def run(self, args: list[str], workspace: Path, patch: str | None = None) -> GitPatchResult: ...


class LocalPatchGit:
    def run(self, args: list[str], workspace: Path, patch: str | None = None) -> GitPatchResult:
        environment = {
            key: os.environ[key]
            for key in ("PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP")
            if key in os.environ
        }
        environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "LC_ALL": "C",
            }
        )
        command = [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.autocrlf=false",
            "-c",
            "protocol.allow=never",
            *args,
        ]
        try:
            result = subprocess.run(
                command,
                cwd=workspace,
                env=environment,
                input=patch.encode("utf-8") if patch is not None else None,
                capture_output=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired:
            raise PatchError("patch_git_timeout") from None
        except OSError:
            raise PatchError("patch_git_unavailable") from None
        if len(result.stdout) > 1048576:
            raise PatchError("patch_diff_too_large")
        return GitPatchResult(
            result.returncode,
            result.stdout.decode("utf-8", errors="replace"),
            result.stderr[:4000].decode("utf-8", errors="replace"),
        )
