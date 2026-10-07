"""Temporary restricted containers; production orchestration does not invoke this adapter yet."""

import json
import logging
import re
from time import monotonic
from uuid import uuid4

from app.core.config import Settings
from app.core.timing import observed
from app.sandbox.base import SandboxError, SandboxRequest, SandboxResult
from app.sandbox.policy import environment_values, workspace_path
from app.sandbox.runner import DockerCLI, DockerRunner

logger = logging.getLogger(__name__)


class DockerSandbox:
    def __init__(self, settings: Settings, runner: DockerRunner | None = None) -> None:
        self.settings = settings.model_copy(deep=True)
        self.runner = runner if runner is not None else DockerCLI()

    def _control(self, args: list[str]) -> str:
        try:
            result = self.runner.run(args, timeout=15, output_limit=1048576)
        except Exception:
            raise SandboxError("docker_operation_failed") from None
        if result.code or result.timed_out or result.truncated:
            raise SandboxError("docker_operation_failed")
        return result.stdout.strip()

    @observed("docker.execute")
    def execute(self, request: SandboxRequest) -> SandboxResult:
        started = monotonic()
        settings = self.settings
        workspace = workspace_path(settings.workspace_root, request.workspace)
        environment = environment_values(request)
        values = [*request.command, *request.environment.values()]
        for secret in (settings.github_token, settings.llm_api_key):
            if secret and any(secret.get_secret_value() in value for value in values):
                raise SandboxError("secret_in_sandbox_input")
        if (
            not request.command
            or request.command[0].startswith("-")
            or any(not part or "\x00" in part or len(part) > 16384 for part in request.command)
        ):
            raise SandboxError("invalid_command")
        reference = settings.sandbox_images.get(request.image)
        if not reference or reference.startswith("-") or any(c.isspace() for c in reference):
            raise SandboxError("untrusted_image")
        if self._control(["info", "--format", "{{.OSType}}"]) != "linux":
            raise SandboxError("linux_docker_required")
        try:
            metadata = json.loads(
                self._control(["image", "inspect", "--format", "{{json .}}", reference])
            )
            image_id = metadata["Id"]
            if (
                not isinstance(image_id, str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
                or metadata["Os"] != "linux"
                or metadata["Config"].get("Volumes")
            ):
                raise SandboxError("unsafe_image")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise SandboxError("unsafe_image") from None
        name = f"platform-sandbox-{uuid4().hex}"
        memory = f"{settings.sandbox_memory_mb}m"
        # Docker CLI adds client-config proxy credentials unless explicitly overridden.
        # Clear them at creation too: env -i alone leaves them in metadata and init's environment.
        proxy_options = [
            argument
            for proxy in ("HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY", "NO_PROXY")
            for key in (proxy, proxy.lower())
            for argument in ("--env", f"{key}=")
        ]
        args = [
            "create",
            "--name",
            name,
            "--label",
            "platform.sandbox=true",
            "--pull=never",
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--user",
            settings.sandbox_user,
            "--read-only",
            "--init",
            "--no-healthcheck",
            "--cpus",
            str(settings.sandbox_cpu_limit),
            "--memory",
            memory,
            "--memory-swap",
            memory,
            "--pids-limit",
            str(settings.sandbox_pid_limit),
            "--restart=no",
            "--ipc=private",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
            "--shm-size=16m",
            "--log-driver=none",
            "--workdir=/workspace",
            "--mount",
            f"type=bind,src={workspace},dst=/workspace,bind-propagation=rprivate,bind-recursive=disabled",
            "--entrypoint=/usr/bin/env",
            *proxy_options,
            image_id,
            "-i",
            *environment,
            *request.command,
        ]
        try:
            self._control(args)
            attached = self.runner.run(
                ["start", "--attach", name],
                timeout=min(request.timeout, settings.sandbox_timeout_seconds),
                output_limit=settings.sandbox_output_bytes,
            )
            exit_code = None
            if not attached.timed_out:
                state = json.loads(self._control(["inspect", "--format", "{{json .State}}", name]))
                if state["Status"] != "exited" or not isinstance(state["ExitCode"], int):
                    raise SandboxError("sandbox_start_failed")
                exit_code = state["ExitCode"]
            result = SandboxResult(
                exit_code=exit_code,
                stdout=attached.stdout,
                stderr=attached.stderr,
                elapsed_seconds=monotonic() - started,
                timed_out=attached.timed_out,
                output_truncated=attached.truncated,
            )
            logger.info(
                "Sandbox finished id=%s exit_code=%s timed_out=%s",
                name,
                exit_code,
                result.timed_out,
            )
            return result
        except SandboxError:
            raise
        except Exception:
            raise SandboxError("sandbox_operation_failed") from None
        finally:
            # Named before create, so a timed-out create can still be cleaned up.
            try:
                cleanup = self.runner.run(["rm", "--force", "--volumes", name], 15, 1024)
            except Exception:
                raise SandboxError("sandbox_cleanup_failed") from None
            if cleanup.code or cleanup.timed_out:
                logger.error("Sandbox cleanup failed id=%s", name)
                raise SandboxError("sandbox_cleanup_failed") from None
