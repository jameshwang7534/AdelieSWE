"""Temporary local Git repositories; never contacts GitHub."""

from pathlib import Path

import pytest

from app.integrations.git.runner import SubprocessGitRunner


@pytest.fixture
def local_repository(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    git = SubprocessGitRunner()
    git.run(["init", "--initial-branch=main"], cwd=source)
    (source / "hello.txt").write_text("original\n", encoding="utf-8")
    (source / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    git.run(["add", "."], cwd=source)
    git.run(
        [
            "-c",
            "user.name=Local Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "Initial fixture",
        ],
        cwd=source,
    )
    return source
