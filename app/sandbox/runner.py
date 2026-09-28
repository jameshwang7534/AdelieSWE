"""Bounded Docker CLI transport, replaceable without Docker in unit tests."""

import os
import shutil
import subprocess
from dataclasses import dataclass
from threading import Thread
from typing import BinaryIO, Protocol

from app.sandbox.base import SandboxError


@dataclass(frozen=True)
class CommandResult:
    code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    truncated: bool = False


class DockerRunner(Protocol):
    def run(self, arguments: list[str], timeout: float, output_limit: int) -> CommandResult: ...


class DockerCLI:
    def __init__(self) -> None:
        executable = shutil.which("docker")
        if executable is None:
            raise SandboxError("docker_cli_unavailable")
        self.executable = executable

    def run(self, arguments: list[str], timeout: float, output_limit: int) -> CommandResult:
        # Only CLI connectivity/config essentials. These are never forwarded into containers.
        names = (
            "PATH",
            "SystemRoot",
            "SYSTEMROOT",
            "WINDIR",
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
            "TEMP",
            "TMP",
        )
        environment = {key: os.environ[key] for key in names if key in os.environ}
        try:
            process = subprocess.Popen(
                [self.executable, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError:
            raise SandboxError("docker_cli_unavailable") from None
        buffers = [bytearray(), bytearray()]
        truncated = [False, False]

        def drain(stream: BinaryIO, index: int) -> None:
            try:
                while block := stream.read(8192):
                    remaining = max(0, output_limit - len(buffers[index]))
                    buffers[index].extend(block[:remaining])
                    if len(block) > remaining:
                        truncated[index] = True
            finally:
                stream.close()

        assert process.stdout is not None and process.stderr is not None
        threads = [
            Thread(target=drain, args=(stream, index), daemon=True)
            for index, stream in enumerate((process.stdout, process.stderr))
        ]
        for thread in threads:
            thread.start()
        expired = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            expired = True
            process.kill()
            process.wait()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            for thread in threads:
                thread.join()
        return CommandResult(
            process.returncode,
            buffers[0].decode("utf-8", errors="replace"),
            buffers[1].decode("utf-8", errors="replace"),
            expired,
            any(truncated),
        )
