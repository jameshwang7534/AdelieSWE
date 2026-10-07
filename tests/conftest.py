"""Temporary local Git repositories; never contacts GitHub."""

import socket
from pathlib import Path
from typing import Any

import pytest

from app.integrations.git.runner import SubprocessGitRunner


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--run-external", action="store_true", default=False)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        category = next(
            (name for name in ("unit", "integration", "e2e") if name in item.path.parts), None
        )
        if category:
            item.add_marker(getattr(pytest.mark, category))
        if item.get_closest_marker("external") and not config.getoption("--run-external"):
            item.add_marker(pytest.mark.skip(reason="Real providers require --run-external"))


@pytest.fixture(autouse=True)
def local_network_only(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Block external Python DNS/network destinations before a connection can occur."""
    if request.node.get_closest_marker("external") and request.config.getoption("--run-external"):
        return
    original = socket.getaddrinfo

    def guarded(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in (None, "localhost", "127.0.0.1", "::1", b"localhost", b"127.0.0.1"):
            raise AssertionError("External network disabled in tests; use a fake provider")
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)


@pytest.fixture
def source_repository(local_repository: Path) -> Path:
    """Reusable mixed-language checkout with excluded/binary content."""
    files = {
        "src/users.py": "def get_user_by_id(user_id):\n    return {'id': user_id}\n",
        "src/client.ts": "export function getUserById(id: number) {\n  return {id};\n}\n",
        "README.md": "# Fixture\nUser lookup examples.\n",
        "node_modules/ignored.js": "generated = true;\n",
    }
    for name, content in files.items():
        path = local_repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    (local_repository / "binary.py").write_bytes(b"binary\x00content")
    return local_repository


@pytest.fixture
def local_repository(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    git = SubprocessGitRunner()
    git.run(["init", "--initial-branch=main"], cwd=source)
    (source / "hello.txt").write_text("original\n", encoding="utf-8", newline="\n")
    (source / ".gitignore").write_text("ignored.txt\n", encoding="utf-8", newline="\n")
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
