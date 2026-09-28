"""Sandbox policy, lifecycle failures, and bounded CLI transport without a daemon."""

import json
import sys
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.sandbox.base import SandboxError, SandboxRequest
from app.sandbox.docker import DockerSandbox
from app.sandbox.runner import CommandResult, DockerCLI


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.output = CommandResult(0, "fixture stdout", "fixture stderr")
        self.exit_code = 7
        self.fail: str | None = None
        self.volumes: dict[str, object] | None = None

    def run(self, arguments: list[str], timeout: float, output_limit: int) -> CommandResult:
        self.calls.append(arguments)
        operation = arguments[0]
        if operation == self.fail:
            return CommandResult(1, stderr="sensitive daemon error")
        if operation == "info":
            return CommandResult(0, "linux")
        if operation == "image":
            return CommandResult(
                0,
                json.dumps(
                    {"Id": "sha256:" + "a" * 64, "Os": "linux", "Config": {"Volumes": self.volumes}}
                ),
            )
        if operation == "start":
            return self.output
        if operation == "inspect":
            return CommandResult(0, json.dumps({"Status": "exited", "ExitCode": self.exit_code}))
        return CommandResult(0)


def setup(tmp_path: Path) -> tuple[Settings, SandboxRequest, FakeDocker]:
    workspace = tmp_path / "executions" / str(uuid4()) / str(uuid4())
    workspace.mkdir(parents=True)
    return (
        Settings(workspace_root=tmp_path, llm_api_key=None, github_token=None),
        SandboxRequest(workspace=workspace, command=("python", "-c", "print('fixture')")),
        FakeDocker(),
    )


def test_restrictions_and_nonzero_exit(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    settings, request, runner = setup(tmp_path)
    request.environment = {"CI": "true"}
    request.environment_allowlist = frozenset({"CI"})
    result = DockerSandbox(settings, runner).execute(request)
    assert result.exit_code == 7 and result.stdout == "fixture stdout"
    assert result.stderr == "fixture stderr" and not result.timed_out
    create = next(call for call in runner.calls if call[0] == "create")
    for option in (
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--read-only",
        "--no-healthcheck",
        "--log-driver=none",
        "--pull=never",
    ):
        assert option in create
    assert "--privileged" not in create and "--publish" not in create
    assert (
        create.count("--mount") == 1
        and "bind-recursive=disabled" in create[create.index("--mount") + 1]
    )
    assert create[create.index("--user") + 1] == "1000:1000"
    assert create[create.index("--memory") + 1] == "256m"
    assert create[create.index("--memory-swap") + 1] == "256m"
    assert create[create.index("--cpus") + 1] == "1.0"
    assert create[create.index("--pids-limit") + 1] == "64"
    assert create[-3:] == list(request.command)
    assert "-i" in create and "CI=true" in create
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY", "NO_PROXY"):
        assert f"{key}=" in create and f"{key.lower()}=" in create
    assert runner.calls[-1][:3] == ["rm", "--force", "--volumes"]
    assert "fixture stdout" not in caplog.text and "print('fixture')" not in caplog.text


@pytest.mark.parametrize("failure", ["create", "start", "inspect", "rm"])
def test_cleanup_after_errors(tmp_path: Path, failure: str) -> None:
    settings, request, runner = setup(tmp_path)
    runner.fail = failure
    if failure == "start":
        # Start failed before a container command was able to exit.
        original = runner.run

        def run(arguments: list[str], timeout: float, output_limit: int) -> CommandResult:
            if arguments[0] == "inspect":
                runner.calls.append(arguments)
                return CommandResult(0, '{"Status":"created","ExitCode":0}')
            return original(arguments, timeout, output_limit)

        with patch.object(runner, "run", side_effect=run), pytest.raises(SandboxError):
            DockerSandbox(settings, runner).execute(request)
    else:
        with pytest.raises(SandboxError) as error:
            DockerSandbox(settings, runner).execute(request)
        assert "sensitive" not in str(error.value)
    assert runner.calls[-1][0] == "rm"


def test_timeout_preserves_bounded_partial_output_and_cleans_up(tmp_path: Path) -> None:
    settings, request, runner = setup(tmp_path)
    runner.output = CommandResult(-1, "partial", "partial error", timed_out=True, truncated=True)
    result = DockerSandbox(settings, runner).execute(request)
    assert result.timed_out and result.exit_code is None and result.output_truncated
    assert result.stdout == "partial" and result.stderr == "partial error"
    assert runner.calls[-1][0] == "rm"


@pytest.mark.parametrize(
    "name", ["GITHUB_TOKEN", "LLM_API_KEY", "PATH", "DOCKER_HOST", "LD_PRELOAD"]
)
def test_environment_deny_overrides_request_allowlist(tmp_path: Path, name: str) -> None:
    settings, request, runner = setup(tmp_path)
    request.environment = {name: "not-allowed"}
    request.environment_allowlist = frozenset({name})
    with pytest.raises(SandboxError, match="environment_not_allowed"):
        DockerSandbox(settings, runner).execute(request)
    assert runner.calls == []


def test_known_credentials_cannot_be_smuggled_in_safe_variable(tmp_path: Path) -> None:
    settings, request, runner = setup(tmp_path)
    settings.llm_api_key = SecretStr("synthetic-secret")
    request.environment = {"CI": "synthetic-secret"}
    request.environment_allowlist = frozenset({"CI"})
    with pytest.raises(SandboxError, match="secret_in_sandbox_input"):
        DockerSandbox(settings, runner).execute(request)


@pytest.mark.parametrize("kind", ["root", "outside", "normalized-root", "sensitive", "hardlink"])
def test_unsafe_workspace(tmp_path: Path, kind: str) -> None:
    settings, request, runner = setup(tmp_path)
    if kind == "root":
        request.workspace = tmp_path
    elif kind == "normalized-root":
        request.workspace = request.workspace / ".."
    elif kind == "outside":
        request.workspace = tmp_path.parent
    elif kind == "sensitive":
        (request.workspace / ".env").write_text("synthetic fixture", encoding="utf-8")
    else:
        source = tmp_path / "external"
        source.write_text("fixture", encoding="utf-8")
        (request.workspace / "linked").hardlink_to(source)
    with pytest.raises(SandboxError):
        DockerSandbox(settings, runner).execute(request)
    assert runner.calls == []


def test_arbitrary_image_options_and_image_volumes_rejected(tmp_path: Path) -> None:
    settings, request, runner = setup(tmp_path)
    request.image = "attacker/image"
    with pytest.raises(SandboxError, match="untrusted_image"):
        DockerSandbox(settings, runner).execute(request)
    request.image = "python"
    runner.volumes = {"/unexpected": {}}
    with pytest.raises(SandboxError, match="unsafe_image"):
        DockerSandbox(settings, runner).execute(request)
    assert all(call[0] != "create" for call in runner.calls)
    with pytest.raises(ValidationError):
        SandboxRequest(workspace=tmp_path, command=("echo",), privileged=True)  # type: ignore[call-arg]


def test_cli_transport_bounds_streams_and_timeout() -> None:
    with patch("app.sandbox.runner.shutil.which", return_value=sys.executable):
        runner = DockerCLI()
    result = runner.run(
        ["-c", "import sys; print('x'*10000); print('y'*10000,file=sys.stderr)"], 5, 1024
    )
    assert result.code == 0 and result.truncated
    assert len(result.stdout) == len(result.stderr) == 1024
    result = runner.run(["-u", "-c", "import time; print('partial'); time.sleep(20)"], 1, 1024)
    assert result.timed_out and "partial" in result.stdout
