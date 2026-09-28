"""Opt-in real restricted Linux container tests; no repository code or AI execution."""

import json
import os
import time
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.sandbox.base import SandboxError, SandboxRequest
from app.sandbox.docker import DockerSandbox
from app.sandbox.runner import CommandResult, DockerCLI

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_SANDBOX_TESTS") != "1",
    reason="Requires Linux Docker and trusted Python image",
)


class InspectingCLI(DockerCLI):
    def __init__(self) -> None:
        super().__init__()
        self.names: list[str] = []
        self.inspected: list[dict[str, object]] = []

    def run(self, arguments: list[str], timeout: float, output_limit: int) -> CommandResult:
        if arguments[0] == "create":
            self.names.append(arguments[arguments.index("--name") + 1])
        if arguments[0] == "rm":
            metadata = super().run(["inspect", arguments[-1]], 15, 1048576)
            self.inspected.append(json.loads(metadata.stdout)[0])
        return super().run(arguments, timeout, output_limit)


def test_real_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "executions" / str(uuid4()) / str(uuid4())
    workspace.mkdir(parents=True)
    workspace.chmod(0o777)
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-not-forwarded")
    settings = Settings(
        workspace_root=tmp_path, sandbox_output_bytes=1024, github_token=None, llm_api_key=None
    )
    runner = InspectingCLI()
    sandbox = DockerSandbox(settings, runner)
    code = (
        "import os,json,sys; "
        "open('/workspace/result.txt','w').write('fixture'); "
        "print(json.dumps({'uid':os.getuid(),'env':dict(os.environ)})); "
        "print('fixture stderr',file=sys.stderr); sys.exit(7)"
    )
    result = sandbox.execute(SandboxRequest(workspace=workspace, command=("python", "-c", code)))
    assert result.exit_code == 7 and not result.timed_out
    output = json.loads(result.stdout)
    assert output["uid"] == 1000 and "GITHUB_TOKEN" not in output["env"]
    assert "fixture stderr" in result.stderr
    assert (workspace / "result.txt").read_text() == "fixture"
    metadata = runner.inspected[0]
    # Docker's actual configuration, not just CLI arguments.
    host = metadata["HostConfig"]
    assert isinstance(host, dict)
    assert not host["Privileged"] and host["ReadonlyRootfs"]
    assert host["NetworkMode"] == "none" and host["CapDrop"] == ["ALL"]
    assert "no-new-privileges:true" in host["SecurityOpt"]
    assert host["Memory"] == 256 * 1024 * 1024 and host["MemorySwap"] == host["Memory"]
    assert host["NanoCpus"] == 1000000000 and host["PidsLimit"] == 64
    assert len(host["Mounts"]) == 1 and host["Mounts"][0]["Target"] == "/workspace"
    timeout = sandbox.execute(
        SandboxRequest(
            workspace=workspace,
            timeout=1,
            command=("python", "-u", "-c", "import time; print('partial'); time.sleep(30)"),
        )
    )
    assert timeout.timed_out and timeout.exit_code is None and "partial" in timeout.stdout
    flood = sandbox.execute(
        SandboxRequest(workspace=workspace, command=("python", "-c", "print('x'*100000)"))
    )
    assert flood.exit_code == 0 and flood.output_truncated and len(flood.stdout) == 1024
    for name in runner.names:
        assert runner.run(["inspect", name], 5, 1024).code != 0


def test_security_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "executions" / str(uuid4()) / str(uuid4())
    workspace.mkdir(parents=True)
    workspace.chmod(0o777)
    outside = workspace.parent / "outside.txt"
    outside.write_text("host-only fixture", encoding="utf-8")
    for name in ("GITHUB_TOKEN", "LLM_API_KEY", "API_KEY"):
        monkeypatch.setenv(name, "synthetic-host-secret")
    runner = InspectingCLI()
    # Use a disposable client configuration, never the user's Docker config/auth file.
    endpoint = runner.run(
        ["context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"], 10, 4096
    )
    assert endpoint.code == 0
    config = tmp_path / "docker-client"
    config.mkdir()
    (config / "config.json").write_text(
        json.dumps(
            {
                "proxies": {
                    "default": {
                        "httpProxy": "http://fixture:synthetic-proxy-secret@example.invalid:3128",
                        "httpsProxy": "http://fixture:synthetic-proxy-secret@example.invalid:3128",
                        "ftpProxy": "http://fixture:synthetic-proxy-secret@example.invalid:3128",
                        "allProxy": "http://fixture:synthetic-proxy-secret@example.invalid:3128",
                        "noProxy": "synthetic-proxy-secret.invalid",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCKER_CONFIG", str(config))
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_HOST", json.loads(endpoint.stdout))
    sandbox = DockerSandbox(
        Settings(workspace_root=tmp_path, github_token=None, llm_api_key=None), runner
    )
    normal = sandbox.execute(
        SandboxRequest(
            workspace=workspace, command=("sh", "-c", "echo normal-output; echo separate-error >&2")
        )
    )
    assert normal.exit_code == 0 and normal.stdout.strip() == "normal-output"
    assert normal.stderr.strip() == "separate-error"
    metadata = runner.inspected[-1]
    assert "synthetic-proxy-secret" not in json.dumps(metadata)
    assert "synthetic-host-secret" not in json.dumps(metadata)
    host = metadata["HostConfig"]
    assert isinstance(host, dict)
    assert not host["Privileged"] and host["ReadonlyRootfs"]
    assert host["NetworkMode"] == "none" and host["CapDrop"] == ["ALL"]
    assert host["NanoCpus"] == 1000000000 and host["Memory"] == 268435456
    assert host["MemorySwap"] == 268435456 and host["PidsLimit"] == 64
    assert not host["Binds"] and not host["VolumesFrom"] and not host["PortBindings"]
    assert len(host["Mounts"]) == 1
    mount = host["Mounts"][0]
    assert mount["Source"] == str(workspace.resolve()) and mount["Target"] == "/workspace"
    assert mount["Type"] == "bind" and mount["BindOptions"]["NonRecursive"]
    probe = """import errno,json,os,socket
from pathlib import Path
s = socket.socket()
s.settimeout(0.5)
try:
    s.connect(("192.0.2.1", 9))
except OSError as error:
    network_error = error.errno
else:
    raise AssertionError("Unexpected network access")
finally:
    s.close()
try:
    Path("/forbidden-write").write_text("fixture")
except OSError as error:
    readonly_error = error.errno
else:
    raise AssertionError("Writable root")
assert not Path("/workspace/../outside.txt").exists()
assert not Path("/var/run/docker.sock").exists()
for name in ("GITHUB_TOKEN", "LLM_API_KEY", "API_KEY", "HTTP_PROXY"):
    assert name not in os.environ
print(json.dumps({"network_error": network_error, "readonly_error": readonly_error,
                  "uid": os.getuid(), "interfaces": os.listdir("/sys/class/net")}))
"""
    checked = sandbox.execute(SandboxRequest(workspace=workspace, command=("python", "-c", probe)))
    assert checked.exit_code == 0, checked.stderr
    evidence = json.loads(checked.stdout)
    assert evidence["network_error"] == 101  # Linux ENETUNREACH, not just a remote refusal.
    assert evidence["interfaces"] == ["lo"] and evidence["uid"] == 1000
    assert evidence["readonly_error"] in (13, 30)
    child = "import time; time.sleep(3); open('/workspace/too-late','w').write('bad')"
    delayed = (
        "import subprocess,time; "
        f"subprocess.Popen(['python','-c',{child!r}]); "
        "print('child-started',flush=True); time.sleep(30)"
    )
    expired = sandbox.execute(
        SandboxRequest(workspace=workspace, timeout=1, command=("python", "-c", delayed))
    )
    assert expired.timed_out and "child-started" in expired.stdout
    time.sleep(3.2)
    assert not (workspace / "too-late").exists()
    count = len(runner.names)
    for path in (workspace / "..", workspace / "../../../..", tmp_path, Path(tmp_path.anchor)):
        with pytest.raises(SandboxError, match="unsafe_workspace"):
            sandbox.execute(SandboxRequest(workspace=path, command=("echo", "denied")))
    assert len(runner.names) == count
    for name in runner.names:
        assert runner.run(["inspect", name], 5, 4096).code != 0
    assert runner.run(["info", "--format", "{{.OSType}}"], 5, 4096).stdout.strip() == "linux"
    print(
        "Audit: shell/stdout/stderr OK; network errno=101; uid=1000; "
        "CPU=1 memory=256MiB PIDs=64; one workspace bind; timeout stopped delayed write; "
        "containers removed; traversal denied; host/proxy secrets absent"
    )
