"""Real PostgreSQL reconciliation and real Redis workers over a temporary local checkout."""

import hashlib
import os
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from celery.contrib.testing.worker import start_worker
from kombu import Queue
from pydantic import SecretStr
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.session import create_session_factory, session_scope
from app.indexing.scanner import RepositoryScanner
from app.indexing.service import IndexingError, IndexingService
from app.integrations.git.runner import SubprocessGitRunner
from app.models import CodeChunk, Repository
from app.services.workspace import WorkspaceService
from app.services.workspace_sync import prepare_registered_repository
from app.workers.factory import create_celery_app
from app.workers.indexing import INDEX_TASK_NAME
from tests.integration.test_database import engine as engine
from tests.integration.test_database import factory as factory

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DATABASE_TESTS") != "1",
    reason="Requires opt-in PostgreSQL tests",
)


def setup_repository(
    sessions: sessionmaker[Session],
    root: Path,
    source: Path,
) -> tuple[UUID, WorkspaceService]:
    (source / "a.py").write_text("def first():\n    return 1\n")
    (source / "stable.md").write_text("# Stable documentation\n")
    git = SubprocessGitRunner()
    git.run(["add", "."], cwd=source)
    git.run(
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "Index fixture",
        ],
        cwd=source,
    )
    with session_scope(sessions) as session:
        record = Repository(
            github_owner="index",
            github_name=str(uuid4()),
            clone_url=str(source),
            default_branch="main",
        )
        session.add(record)
        session.flush()
        identifier = record.id
    workspace = WorkspaceService(root, git, allow_local=True)
    prepare_registered_repository(identifier, sessions, workspace)
    return identifier, workspace


def snapshots(sessions: sessionmaker[Session], identifier: UUID) -> dict[str, tuple[UUID, str]]:
    with session_scope(sessions) as session:
        records = session.scalars(
            select(CodeChunk).where(CodeChunk.repository_id == identifier)
        ).all()
        assert all(record.embedding is None for record in records)
        return {record.file_path: (record.id, record.content) for record in records}


def test_reconciliation_and_failure_atomicity(
    factory: sessionmaker[Session],
    tmp_path: Path,
    local_repository: Path,
) -> None:
    identifier, workspace = setup_repository(factory, tmp_path / "workspaces", local_repository)
    scanner = RepositoryScanner()
    service = IndexingService(factory, workspace, scanner)
    assert service.index(identifier)["chunks"] == 2
    first = snapshots(factory, identifier)
    assert service.index(identifier)["chunks"] == 2
    assert snapshots(factory, identifier) == first
    root = workspace.repository_path(identifier)
    (root / "a.py").write_text("def first():\n    return 2\n")
    (root / "new.ts").write_text("export const count = 1;\n")
    service.index(identifier)
    changed = snapshots(factory, identifier)
    assert changed["stable.md"] == first["stable.md"]
    assert changed["a.py"][0] != first["a.py"][0]
    assert "return 2" in changed["a.py"][1]
    (root / "new.ts").unlink()
    service.index(identifier)
    assert "new.ts" not in snapshots(factory, identifier)
    before_failure = snapshots(factory, identifier)
    with patch.object(scanner, "scan", side_effect=OSError("synthetic-sensitive-error")):
        with pytest.raises(IndexingError, match="repository_indexing_failed"):
            service.index(identifier)
    assert snapshots(factory, identifier) == before_failure
    with session_scope(factory) as session:
        record = session.get(Repository, identifier)
        assert record is not None and record.index_status == "error"
    # Force a failure after reconciliation changes are staged: the transaction must roll back.
    (root / "a.py").write_text("updated\n")
    original_flush = Session.flush

    def fail_chunk_write(session: Session, objects: object = None) -> None:
        if any(isinstance(record, CodeChunk) for record in session.new | session.deleted):
            raise RuntimeError("synthetic-write-failure")
        original_flush(session)

    with patch.object(Session, "flush", fail_chunk_write):
        with pytest.raises(IndexingError):
            service.index(identifier)
    assert snapshots(factory, identifier) == before_failure
    (root / "a.py").unlink()
    (root / "stable.md").unlink()
    assert service.index(identifier)["chunks"] == 0
    assert snapshots(factory, identifier) == {}


@pytest.mark.skipif(os.environ.get("RUN_WORKER_TESTS") != "1", reason="Requires Redis")
def test_indexing_through_worker(engine: Engine, tmp_path: Path, local_repository: Path) -> None:
    sessions = create_session_factory(engine)
    identifier, workspace = setup_repository(sessions, tmp_path / "workspaces", local_repository)
    root = workspace.repository_path(identifier)
    (root / "stable.md").unlink()
    (root / "client.ts").write_text("export function value() {\n  return 1;\n}\n")
    (root / "README.md").write_text("# Fixture\nPython and TypeScript.\nAudit documentation.\n")
    (root / "deleted.py").write_text("remove_me = True\n")
    (root / "binary.bin").write_bytes(b"\x00\xff\x80")
    (root / "binary.py").write_bytes(b"binary\x00payload")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "ignored.ts").write_text("export const ignored = true;\n")
    settings = Settings(
        database_url=SecretStr(engine.url.render_as_string(hide_password=False)),
        workspace_root=workspace.root,
        index_chunk_max_lines=2,
    )

    def load_chunks() -> list[CodeChunk]:
        with session_scope(sessions) as session:
            return list(
                session.scalars(
                    select(CodeChunk)
                    .where(CodeChunk.repository_id == identifier)
                    .order_by(CodeChunk.file_path, CodeChunk.start_line)
                )
            )

    def verify_chunks(chunks: list[CodeChunk], expected_files: set[str]) -> None:
        assert {chunk.file_path for chunk in chunks} == expected_files
        identities = [(chunk.file_path, chunk.start_line, chunk.end_line) for chunk in chunks]
        assert len(identities) == len(set(identities))
        for chunk in chunks:
            lines = (root / chunk.file_path).read_bytes().decode("utf-8").splitlines(keepends=True)
            assert 1 <= chunk.start_line <= chunk.end_line <= len(lines)
            assert chunk.end_line - chunk.start_line < 2
            assert chunk.content == "".join(lines[chunk.start_line - 1 : chunk.end_line])
            assert chunk.content_hash == hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
            assert chunk.embedding is None
        for name in expected_files:
            selected = [chunk for chunk in chunks if chunk.file_path == name]
            assert selected[0].start_line == 1
            assert "".join(chunk.content for chunk in selected) == (
                root / name
            ).read_bytes().decode("utf-8")

    app = create_celery_app(settings)
    queue = f"index-test-{uuid4().hex}"
    app.conf.task_queues = (Queue(queue),)
    app.conf.task_default_queue = queue
    app.conf.task_routes = {INDEX_TASK_NAME: {"queue": queue}}
    task_ids: list[str] = []
    try:
        with (
            patch("app.workers.indexing.Settings", return_value=settings),
            start_worker(
                app,
                pool="solo",
                perform_ping_check=False,
                queues=[queue],
            ),
        ):
            counts: list[int] = []
            expected_files = {"a.py", "client.ts", "README.md", "deleted.py"}
            initial: list[CodeChunk] = []
            for attempt in range(2):
                task = app.send_task(INDEX_TASK_NAME, args=[str(identifier)])
                task_ids.append(str(task.id))
                result = task.get(timeout=45)
                assert result["files"] == 4 and result["chunks"] == 6
                assert task.state == "SUCCESS"
                counts.append(result["chunks"])
                current = load_chunks()
                assert len(current) == 6
                verify_chunks(current, expected_files)
                if attempt == 0:
                    initial = current
                else:
                    assert [(c.id, c.content_hash) for c in current] == [
                        (c.id, c.content_hash) for c in initial
                    ]
            stable_ids = {c.id for c in initial if c.file_path != "a.py"}
            original_changed_ids = {c.id for c in initial if c.file_path == "a.py"}
            (root / "a.py").write_text(
                "def first():\n    value = 2\n    value += 1\n    return value\n"
            )
            task = app.send_task(INDEX_TASK_NAME, args=[str(identifier)])
            task_ids.append(str(task.id))
            counts.append(task.get(timeout=45)["chunks"])
            changed = load_chunks()
            assert task.state == "SUCCESS" and len(changed) == 7
            verify_chunks(changed, expected_files)
            assert {c.id for c in changed if c.file_path != "a.py"} == stable_ids
            assert not original_changed_ids & {c.id for c in changed}
            (root / "deleted.py").unlink()
            task = app.send_task(INDEX_TASK_NAME, args=[str(identifier)])
            task_ids.append(str(task.id))
            counts.append(task.get(timeout=45)["chunks"])
            remaining = load_chunks()
            assert task.state == "SUCCESS" and len(remaining) == 6
            verify_chunks(remaining, expected_files - {"deleted.py"})
            assert counts == [6, 6, 7, 6]
            with session_scope(sessions) as session:
                record = session.get(Repository, identifier)
                assert record is not None and record.index_status == "ready"
            with patch.object(
                RepositoryScanner, "scan", side_effect=OSError("synthetic-read-error")
            ):
                task = app.send_task(INDEX_TASK_NAME, args=[str(identifier)])
                task_ids.append(str(task.id))
                task.get(timeout=45, propagate=False)
                assert task.state == "FAILURE"
            assert [(c.id, c.content_hash) for c in load_chunks()] == [
                (c.id, c.content_hash) for c in remaining
            ]
            with session_scope(sessions) as session:
                record = session.get(Repository, identifier)
                assert record is not None and record.index_status == "error"
            print(
                f"Index audit: counts={counts}; binary=0; ignored=0; "
                "retained_after_failure=6; worker_successes=4; persisted_failures=1; "
                f"tasks={task_ids}"
            )
    finally:
        try:
            for task_id in task_ids:
                app.backend.forget(task_id)
            with app.connection_for_write() as connection:
                Queue(queue)(connection.default_channel).delete(if_unused=True, if_empty=True)
        finally:
            app.backend.result_consumer.stop()
            app.backend.client.close()
            app.close()
            with session_scope(sessions) as session:
                session.execute(delete(CodeChunk).where(CodeChunk.repository_id == identifier))
                session.execute(delete(Repository).where(Repository.id == identifier))
