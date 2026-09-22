"""Bounded subprocess adapter; raw Git errors and command lines are never logged."""

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from pydantic import SecretStr


class WorkspaceError(Exception):
    """Only fixed, credential-free error codes may be exposed to callers."""


class GitRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        cwd: Path,
        token: SecretStr | None = None,
        local: bool = False,
    ) -> str: ...


@contextmanager
def credential_environment(token: SecretStr | None) -> Iterator[dict[str, str]]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("GIT_", "GITHUB_", "PLATFORM_GIT_"))
    }
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GCM_INTERACTIVE": "never",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "LC_ALL": "C",
            "PLATFORM_GIT_PYTHON": Path(sys.executable).as_posix(),
            "PLATFORM_GIT_HELPER": Path(__file__).with_name("askpass.py").as_posix(),
        }
    )
    if token is not None:
        env["PLATFORM_GIT_TOKEN"] = token.get_secret_value()
    with TemporaryDirectory(prefix="platform-askpass-") as directory:
        helper = Path(directory) / "askpass.sh"
        helper.write_text(
            '#!/bin/sh\nexec "$PLATFORM_GIT_PYTHON" "$PLATFORM_GIT_HELPER" "$@"\n',
            encoding="utf-8",
            newline="\n",
        )
        helper.chmod(0o700)
        env["GIT_ASKPASS"] = helper.as_posix()
        yield env


class SubprocessGitRunner:
    def __init__(self, timeout: int = 120) -> None:
        self.timeout = timeout

    def run(
        self,
        args: list[str],
        *,
        cwd: Path,
        token: SecretStr | None = None,
        local: bool = False,
    ) -> str:
        command = [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "http.followRedirects=false",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.https.allow=always",
            "-c",
            f"protocol.file.allow={'always' if local else 'never'}",
            "-c",
            "submodule.recurse=false",
            "-c",
            "core.symlinks=false",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "maintenance.auto=false",
            *args,
        ]
        try:
            with credential_environment(token) as env:
                result = subprocess.run(
                    command,
                    cwd=cwd,
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
        except subprocess.TimeoutExpired:
            raise WorkspaceError("git_timeout") from None
        except OSError:
            raise WorkspaceError("git_unavailable") from None
        if result.returncode:
            raise WorkspaceError("git_operation_failed")
        return result.stdout.strip()
