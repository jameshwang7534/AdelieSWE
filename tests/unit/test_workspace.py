"""Real local Git checkouts, boundary checks, and credential-safe subprocess contracts."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.integrations.git import askpass
from app.integrations.git.runner import SubprocessGitRunner, WorkspaceError, credential_environment
from app.services.workspace import WorkspaceService
from app.workers.factory import create_celery_app
from app.workers.workspaces import PREPARE_TASK_NAME


def test_clone_update_clean_and_isolated_copies(tmp_path: Path, local_repository: Path) -> None:
    git = SubprocessGitRunner()
    service = WorkspaceService(tmp_path / "workspaces", git, allow_local=True)
    identifier = uuid4()
    states: list[str] = []
    first = service.prepare(identifier, str(local_repository), "main", states.append)
    assert states == ["syncing", "ready"]
    assert first.path == service.repository_path(identifier)
    assert (first.path / "hello.txt").read_text() == "original\n"
    assert git.run(["branch", "--show-current"], cwd=first.path) == "main"
    execution = service.create_execution(identifier, uuid4())
    another = service.create_execution(identifier, uuid4())
    assert execution.path != another.path != first.path
    assert execution.commit == first.commit
    assert not (execution.path / ".git/objects/info/alternates").exists()
    assert git.run(["remote"], cwd=execution.path) == ""
    (execution.path / "hello.txt").write_text("execution only")
    assert (first.path / "hello.txt").read_text() == "original\n"
    assert (another.path / "hello.txt").read_text() == "original\n"
    with pytest.raises(WorkspaceError, match="execution_workspace_exists"):
        service.create_execution(identifier, UUID(execution.path.name))
    (first.path / "hello.txt").write_text("dirty")
    (first.path / "untracked.txt").write_text("discard")
    (first.path / "ignored.txt").write_text("discard")
    (local_repository / "hello.txt").write_text("upstream update\n")
    git.run(["add", "."], cwd=local_repository)
    git.run(
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "Update",
        ],
        cwd=local_repository,
    )
    updated = service.prepare(identifier, str(local_repository), "main")
    assert updated.commit != first.commit and updated.path == first.path
    assert (updated.path / "hello.txt").read_text() == "upstream update\n"
    assert not (updated.path / "untracked.txt").exists()
    assert not (updated.path / "ignored.txt").exists()
    assert git.run(["status", "--porcelain"], cwd=updated.path) == ""
    assert service.verify(execution.path) == first.commit
    assert service.prepare(identifier, str(local_repository), "main").commit == updated.commit
    assert service.reset(identifier, first.commit).commit == first.commit
    assert (first.path / "hello.txt").read_text() == "original\n"
    with pytest.raises(WorkspaceError, match="invalid_commit"):
        service.reset(identifier, "--upload-pack=bad")


def test_failure_integrity_and_lock(tmp_path: Path, local_repository: Path) -> None:
    service = WorkspaceService(tmp_path / "workspaces", SubprocessGitRunner(), allow_local=True)
    identifier = uuid4()
    result = service.prepare(identifier, str(local_repository), "main")
    lock = service.root / "locks" / f"{identifier}.lock"
    lock.write_text("another worker")
    with pytest.raises(WorkspaceError, match="workspace_busy"):
        service.prepare(identifier, str(local_repository), "main")
    lock.unlink()
    (result.path / ".git/HEAD").write_text("invalid repository")
    states: list[str] = []
    with pytest.raises(WorkspaceError):
        service.prepare(identifier, str(local_repository), "main", states.append)
    assert states == ["syncing", "error"] and not lock.exists()


@pytest.mark.parametrize(
    "source",
    [
        "https://token@github.com/owner/repo.git",
        "https://evil.invalid/owner/repo.git",
        "ssh://git@github.com/owner/repo",
        "-upload-pack=evil",
        "../outside",
    ],
)
def test_reject_untrusted_sources(tmp_path: Path, source: str) -> None:
    git = Mock()
    service = WorkspaceService(tmp_path / "workspaces", git)
    with pytest.raises(WorkspaceError, match="untrusted_clone_url"):
        service.prepare(uuid4(), source, "main")
    git.run.assert_not_called()


def test_path_boundaries(tmp_path: Path) -> None:
    service = WorkspaceService(tmp_path / "workspaces", Mock())
    with pytest.raises(ValueError):
        service.repository_path("../../outside")  # type: ignore[arg-type]
    with pytest.raises(WorkspaceError, match="unsafe_workspace_path"):
        service.verify(tmp_path)
    with pytest.raises(WorkspaceError, match="unsafe_workspace_path"):
        service.verify(service.root / ".." / "outside")
    with patch.object(Path, "is_junction", return_value=True):
        with pytest.raises(WorkspaceError, match="unsafe_workspace_path"):
            service.repository_path(uuid4())


def test_default_branch_change_and_invalid_source(tmp_path: Path, local_repository: Path) -> None:
    git = SubprocessGitRunner()
    service = WorkspaceService(tmp_path / "workspaces", git, allow_local=True)
    identifier = uuid4()
    service.prepare(identifier, str(local_repository), "main")
    git.run(["checkout", "-b", "release/next"], cwd=local_repository)
    result = service.prepare(identifier, str(local_repository), "release/next")
    assert git.run(["branch", "--show-current"], cwd=result.path) == "release/next"
    invalid = tmp_path / "not-a-repository"
    invalid.mkdir()
    states: list[str] = []
    new_id = uuid4()
    with pytest.raises(WorkspaceError, match="git_operation_failed"):
        service.prepare(new_id, str(invalid), "main", states.append)
    assert states == ["syncing", "error"]
    assert not service.repository_path(new_id).exists()
    assert not (service.root / "locks" / f"{new_id}.lock").exists()


def test_subprocess_credentials_and_error_redaction(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    token = "synthetic-private-git-token"
    with patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 1, token, token)
        with pytest.raises(WorkspaceError) as error:
            SubprocessGitRunner().run(["fetch", "origin"], cwd=tmp_path, token=SecretStr(token))
        assert token not in str(error.value) + caplog.text
        assert token not in str(run.call_args.args)
        options = run.call_args.kwargs
        assert options["env"]["PLATFORM_GIT_TOKEN"] == token
        assert options["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert "credential.helper=" in run.call_args.args[0]
        assert "http.followRedirects=false" in run.call_args.args[0]
    # Exercise Git's real askpass invocation (including Windows paths containing spaces),
    # without making a network request or storing credentials in a helper.
    with credential_environment(SecretStr(token)) as env:
        credential = subprocess.run(
            ["git", "-c", "credential.helper=", "credential", "fill"],
            cwd=tmp_path,
            env=env,
            input="protocol=https\nhost=github.invalid\n\n",
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    assert f"password={token}" in credential.stdout
    assert "username=x-access-token" in credential.stdout
    for prompt, expected in [("Username", "x-access-token"), ("Password", token)]:
        result = subprocess.run(
            [sys.executable, str(Path(askpass.__file__)), prompt],
            env={**os.environ, "PLATFORM_GIT_TOKEN": token},
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == expected


def test_workspace_settings_and_task_registration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "custom"))
    settings = Settings(
        celery_broker_url=SecretStr("redis://localhost/1"),
        celery_result_backend=SecretStr("redis://localhost/2"),
    )
    assert settings.workspace_root == tmp_path / "custom"
    application = create_celery_app(settings)
    try:
        task = application.tasks[PREPARE_TASK_NAME]
        assert application.conf.task_routes[PREPARE_TASK_NAME] == {"queue": "orchestration"}
        assert task.time_limit == 960
        with patch("app.services.workspace_sync.prepare_registered_repository") as prepare:
            with patch("app.db.session.create_database_engine") as engine:
                prepare.return_value.commit = "abc"
                result = task.apply(args=[str(uuid4())])
                assert result.successful() and result.result["commit"] == "abc"
                engine.return_value.dispose.assert_called_once()
    finally:
        application.close()
