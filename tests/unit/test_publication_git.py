"""Local commit/ref safety and credential transport; no network or external API."""

import subprocess
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.integrations.git.patches import LocalPatchGit
from app.integrations.git.runner import GitRunner, SubprocessGitRunner, WorkspaceError
from app.pull_requests.contracts import Publication, PublicationError
from app.pull_requests.git import LocalPublicationGit
from app.pull_requests.resources import publication_resources
from app.services.code_patches import CodePatchService


def test_publication_commit_preserves_default_head_index_and_diff(local_repository: Path) -> None:
    git = LocalPatchGit()
    identifier = uuid4()
    branch = f"ai-platform/issue-1-{identifier.hex}"
    before = git.run(["rev-parse", "main"], local_repository).stdout.strip()
    index = (local_repository / ".git/index").read_bytes()
    (local_repository / "hello.txt").write_bytes(b"changed\n")
    (local_repository / "new.py").write_bytes(b"value = 1\n")
    patches = CodePatchService(local_repository.parent)
    diff = patches.current_diff(local_repository)
    publisher = LocalPublicationGit(Settings())
    parent, commit, files = publisher.prepare(local_repository, branch, identifier)
    assert parent == before and commit != parent and files == ["hello.txt", "new.py"]
    assert git.run(["rev-parse", "main"], local_repository).stdout.strip() == before
    assert git.run(["rev-parse", "HEAD"], local_repository).stdout.strip() == before
    assert git.run(["show", f"{commit}:new.py"], local_repository).stdout == "value = 1\n"
    assert git.run(["rev-parse", branch], local_repository).stdout.strip() == commit
    assert (
        git.run(["rev-list", "--count", f"{parent}..{commit}"], local_repository).stdout.strip()
        == "1"
    )
    assert git.run(["diff", "--name-status", parent, commit], local_repository).stdout == (
        "M\thello.txt\nA\tnew.py\n"
    )
    assert git.run(["show", f"{commit}:hello.txt"], local_repository).stdout == "changed\n"
    assert (local_repository / ".git/index").read_bytes() == index
    assert patches.current_diff(local_repository) == diff
    assert publisher.prepare(local_repository, branch, identifier) == (parent, commit, files)
    (local_repository / "hello.txt").write_bytes(b"different\n")
    with pytest.raises(PublicationError, match="local_branch_collision"):
        publisher.prepare(local_repository, branch, identifier)


def test_push_uses_create_only_lease_and_separate_credentials(tmp_path: Path) -> None:
    remote = Mock(spec=GitRunner)
    token = SecretStr("fixture-credential-not-real")
    settings = Settings(github_token=token)
    identifier = uuid4()
    publication = Publication(
        execution_id=identifier,
        review_id=uuid4(),
        owner="example",
        repository="repo",
        source="https://github.com/example/repo.git",
        base="main",
        branch=f"ai-platform/issue-1-{identifier.hex}",
        parent="a" * 40,
        commit="b" * 40,
        diff_hash="c" * 64,
        title="Change",
        body="Evidence",
    )
    publisher = LocalPublicationGit(settings, remote=remote)
    publisher.push(tmp_path, publication)
    args = remote.run.call_args.args[0]
    assert f"--force-with-lease=refs/heads/{publication.branch}:" in args
    assert args[-1] == f"{'b' * 40}:refs/heads/{publication.branch}"
    assert "fixture-credential-not-real" not in str(args)
    assert remote.run.call_args.kwargs["token"] is token
    publication.branch = "main"
    with pytest.raises(PublicationError, match="default_branch"):
        publisher.push(tmp_path, publication)
    assert remote.run.call_count == 1


def test_missing_credentials_fail_before_resources_connect() -> None:
    with pytest.raises(PublicationError, match="github_token_required"):
        with publication_resources(Settings(github_token=None)):
            pytest.fail("Unconfigured production publication must not start")


@pytest.mark.parametrize("exit_code", [0, 1])
def test_push_discards_remote_output_and_sanitizes_failures(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    exit_code: int,
) -> None:
    credential = "synthetic-audit-token"
    identifier = uuid4()
    publication = Publication(
        execution_id=identifier,
        review_id=uuid4(),
        owner="example",
        repository="repo",
        source="https://github.com/example/repo.git",
        base="main",
        branch=f"ai-platform/issue-1-{identifier.hex}",
        parent="a" * 40,
        commit="b" * 40,
        diff_hash="c" * 64,
        title="Change",
        body="Evidence",
    )
    publisher = LocalPublicationGit(
        Settings(github_token=SecretStr(credential)), remote=SubprocessGitRunner()
    )
    # No subprocess executes: even a hostile remote echoing a credential cannot leak it.
    with patch(
        "app.integrations.git.runner.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[],
            returncode=exit_code,
            stdout=credential,
            stderr=credential,
        ),
    ) as process:
        if exit_code:
            with pytest.raises(WorkspaceError, match="^git_operation_failed$") as error:
                publisher.push(tmp_path, publication)
            assert credential not in str(error.value)
        else:
            publisher.push(tmp_path, publication)
        assert credential not in str(process.call_args.args[0])
        assert process.call_args.kwargs["env"]["PLATFORM_GIT_TOKEN"] == credential
    output = capsys.readouterr()
    assert credential not in output.out + output.err + caplog.text
