"""Scanner and chunk boundaries are deterministic and require no external service."""

import hashlib
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.indexing.chunking import chunk_source
from app.indexing.scanner import EXCLUDED_DIRECTORIES, LANGUAGES, RepositoryScanner, SourceFile
from app.workers.factory import create_celery_app
from app.workers.indexing import INDEX_TASK_NAME


def test_scanner_exclusions_and_languages(local_repository: Path) -> None:
    for directory in EXCLUDED_DIRECTORIES - {".git"}:
        path = local_repository / directory
        path.mkdir()
        (path / "hidden.py").write_text("hidden = True")
    for extension in LANGUAGES:
        (local_repository / f"source{extension}").write_text("source text\n", encoding="utf-8")
    for name, content in {
        ".env.json": "private",
        "credentials.json": "private",
        "secrets.yaml": "private",
        "service-account.json": "private",
        "bundle.min.js": "generated",
        "package-lock.json": "generated",
        "schema_pb2.py": "generated",
        "generated.py": "# DO NOT EDIT\nx = 1",
        "private.py": "-----BEGIN PRIVATE KEY-----",
        "token.py": "ghp_" + "x" * 30,
        "long.py": "x" * 81,
        "large.py": "x\n" * 101,
        "empty.py": " \n",
    }.items():
        (local_repository / name).write_text(content, encoding="utf-8")
    (local_repository / "binary.py").write_bytes(b"a\x00b")
    (local_repository / "encoding.py").write_bytes(b"\xff\xfe")
    scanner = RepositoryScanner(max_file_bytes=200, max_line_chars=80)
    result = list(scanner.scan(local_repository))
    assert {file.path for file in result} == {f"source{ext}" for ext in LANGUAGES}
    assert {file.language for file in result} == set(LANGUAGES.values())
    assert result == list(scanner.scan(local_repository))


def test_size_boundary_and_nested_relative_paths(tmp_path: Path) -> None:
    nested = tmp_path / "src"
    nested.mkdir()
    (nested / "a.py").write_bytes(b"12345")
    assert list(RepositoryScanner(5).scan(tmp_path))[0].path == "src/a.py"
    assert list(RepositoryScanner(4).scan(tmp_path)) == []


def test_chunk_content_lines_hashes_and_python_boundaries() -> None:
    content = "def first():\n    a = 1\n    return a\n\n@decorator\ndef second():\n    return 2\n"
    source = SourceFile("src/sample.py", "python", content)
    chunks = chunk_source(source, max_lines=6, max_chars=100)
    assert [(chunk.start_line, chunk.end_line) for chunk in chunks] == [(1, 4), (5, 7)]
    assert "".join(chunk.content for chunk in chunks) == content
    assert chunks == chunk_source(source, 6, 100)
    for chunk in chunks:
        assert chunk.content_hash == hashlib.sha256(chunk.content.encode()).hexdigest()
        assert chunk.file_path == source.path and chunk.language == "python"


@pytest.mark.parametrize("content", ["x\r\ny\r\nz", "def broken(\nx=1\ny=2\n", "a\n" * 50])
def test_chunk_limits_and_incomplete_source(content: str) -> None:
    chunks = chunk_source(SourceFile("a.py", "python", content), max_lines=3, max_chars=15)
    assert "".join(chunk.content for chunk in chunks) == content
    for index, chunk in enumerate(chunks):
        assert chunk.end_line - chunk.start_line < 3
        assert len(chunk.content) <= 15
        if index:
            assert chunk.start_line == chunks[index - 1].end_line + 1


def test_settings_limits_and_task_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INDEX_MAX_FILE_BYTES", "4096")
    settings = Settings(
        celery_broker_url=SecretStr("redis://localhost/1"),
        celery_result_backend=SecretStr("redis://localhost/2"),
    )
    assert settings.index_max_file_bytes == 4096
    with pytest.raises(ValidationError):
        Settings(index_chunk_max_chars=0)
    app = create_celery_app(settings)
    try:
        assert app.conf.task_routes[INDEX_TASK_NAME] == {"queue": "indexing"}
        assert app.tasks[INDEX_TASK_NAME].time_limit == 960
    finally:
        app.close()
