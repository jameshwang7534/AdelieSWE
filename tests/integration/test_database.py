"""Real PostgreSQL tests, isolated in an automatically removed temporary database."""

import os
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base, Record
from app.db.session import session_scope
from app.models import (
    AgentRun,
    CodeChunk,
    ExecutionRun,
    ImplementationPlan,
    Issue,
    PlanTask,
    PullRequest,
    Repository,
    TaskExecution,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DATABASE_TESTS") != "1",
    reason="Set RUN_DATABASE_TESTS=1 and TEST_DATABASE_URL for PostgreSQL migration tests.",
)
ROOT = Path(__file__).resolve().parents[2]


def migration_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return config


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.fail("TEST_DATABASE_URL must identify a PostgreSQL server with CREATEDB permission")
    url = make_url(value)
    assert url.drivername == "postgresql+psycopg"
    name = f"platform_test_{uuid4().hex}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    database = create_engine(url.set(database=name), hide_parameters=True)
    created = False
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
            created = True
        with database.begin() as connection:
            assert (
                connection.scalar(text("SELECT count(*) FROM pg_extension WHERE extname='vector'"))
                == 0
            )
            config = migration_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            assert (
                connection.scalar(text("SELECT count(*) FROM pg_extension WHERE extname='vector'"))
                == 1
            )
        yield database
    finally:
        database.dispose()
        try:
            if created:
                # Only the UUID-named database created by this fixture is removed.
                with admin.connect() as connection:
                    connection.execute(text(f'DROP DATABASE "{name}"'))
        finally:
            admin.dispose()


@pytest.fixture
def factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield sessionmaker(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
        finally:
            transaction.rollback()


@pytest.fixture
def graph(factory: sessionmaker[Session]) -> dict[str, UUID]:
    ids: dict[str, UUID] = {}
    with session_scope(factory) as session:
        repo = Repository(
            github_owner="example",
            github_name="sample",
            clone_url="https://example.invalid/repo.git",
            default_branch="main",
        )
        session.add(repo)
        session.flush()
        ids["repo"] = repo.id
        issue = Issue(
            repository_id=repo.id,
            github_issue_number=1,
            title="Test issue",
            source_url="https://example.invalid/issues/1",
        )
        session.add(issue)
        session.flush()
        ids["issue"] = issue.id
        plan = ImplementationPlan(issue_id=issue.id, summary="Test plan")
        session.add(plan)
        session.flush()
        ids["plan"] = plan.id
        task = PlanTask(
            plan_id=plan.id, task_key="task-1", title="Test task", target_files=["a.py"]
        )
        session.add(task)
        session.flush()
        ids["task"] = task.id
        run = ExecutionRun(plan_id=plan.id, branch_name="test-branch", started_at=datetime.now(UTC))
        session.add(run)
        session.flush()
        ids["run"] = run.id
        execution = TaskExecution(execution_run_id=run.id, plan_task_id=task.id)
        session.add(execution)
        session.flush()
        ids["execution"] = execution.id
        agent = AgentRun(
            execution_run_id=run.id,
            task_execution_id=execution.id,
            agent_type="coding",
            input_metadata={"files": ["a.py"]},
            input_tokens=10,
            cost_usd=Decimal("0.00001234"),
        )
        chunk = CodeChunk(
            repository_id=repo.id,
            file_path="a.py",
            start_line=1,
            end_line=2,
            content="example",
            content_hash="a" * 64,
            embedding=[0.5] * 1536,
        )
        pr = PullRequest(
            execution_run_id=run.id, github_pr_number=1, github_url="https://example.invalid/pull/1"
        )
        session.add_all([agent, chunk, pr])
        session.flush()
        ids.update(agent=agent.id, chunk=chunk.id, pr=pr.id)
    return ids


def test_roundtrip_all_models(factory: sessionmaker[Session], graph: dict[str, UUID]) -> None:
    with session_scope(factory) as session:
        for model in (
            Repository,
            Issue,
            CodeChunk,
            ImplementationPlan,
            PlanTask,
            ExecutionRun,
            TaskExecution,
            AgentRun,
            PullRequest,
        ):
            record = session.scalars(select(model)).one()
            assert isinstance(record, Record)
            assert isinstance(record.id, UUID)
            assert record.created_at.utcoffset() is not None
            assert record.updated_at.utcoffset() is not None
        chunk = session.get(CodeChunk, graph["chunk"])
        assert chunk is not None and chunk.embedding == [0.5] * 1536
        task = session.get(PlanTask, graph["task"])
        assert task is not None and task.dependencies == []
        task.dependencies.append("task-0")
        agent = session.get(AgentRun, graph["agent"])
        assert agent is not None and agent.cost_usd == Decimal("0.00001234")
        assert agent.input_metadata == {"files": ["a.py"]}
        assert agent.output_tokens is None
        agent.output_metadata["result"] = "ok"
        session.add(AgentRun(execution_run_id=graph["run"], agent_type="review"))
    with session_scope(factory) as session:
        assert session.scalar(select(PlanTask.dependencies)) == ["task-0"]
        stored_agent = session.get(AgentRun, graph["agent"])
        assert stored_agent is not None and stored_agent.output_metadata == {"result": "ok"}


def test_updated_at_trigger_and_server_uuid(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        row = session.execute(
            text("""
            INSERT INTO repositories (github_owner, github_name, clone_url, default_branch)
            VALUES ('raw', 'sql', 'https://example.invalid/raw', 'main')
            RETURNING id, created_at, updated_at
        """)
        ).one()
        assert isinstance(row.id, UUID)
        updated = session.execute(
            text("""
            UPDATE repositories SET default_branch='develop' WHERE id=:id RETURNING updated_at
        """),
            {"id": row.id},
        ).scalar_one()
        assert updated > row.updated_at


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE code_chunks SET start_line=0",
        "UPDATE task_executions SET attempt=0",
        "UPDATE agent_runs SET cost_usd=-1",
        "UPDATE plan_tasks SET dependencies='{}'::jsonb",
        "UPDATE issues SET repository_id=gen_random_uuid()",
        "INSERT INTO repositories (github_owner, github_name, clone_url, default_branch) "
        "VALUES ('EXAMPLE', 'SAMPLE', 'https://example.invalid/duplicate', 'main')",
        "INSERT INTO issues (repository_id, github_issue_number, title, source_url) "
        "SELECT repository_id, github_issue_number, title, source_url FROM issues",
        "INSERT INTO plan_tasks (plan_id, task_key, title) "
        "SELECT plan_id, task_key, title FROM plan_tasks",
        "INSERT INTO task_executions (execution_run_id, plan_task_id, attempt) "
        "SELECT execution_run_id, plan_task_id, attempt FROM task_executions",
        "DELETE FROM repositories",
    ],
)
def test_constraints(factory: sessionmaker[Session], graph: dict[str, UUID], sql: str) -> None:
    with pytest.raises(IntegrityError), session_scope(factory) as session:
        session.execute(text(sql))
    with session_scope(factory) as session:
        assert session.get(Repository, graph["repo"]) is not None


def test_vector_dimension_enforced(factory: sessionmaker[Session], graph: dict[str, UUID]) -> None:
    with pytest.raises(DBAPIError), session_scope(factory) as session:
        session.execute(text("UPDATE code_chunks SET embedding='[1,2,3]'::vector"))


def test_session_rollback(factory: sessionmaker[Session]) -> None:
    with pytest.raises(RuntimeError, match="abort"), session_scope(factory) as session:
        session.add(
            Repository(
                github_owner="rollback",
                github_name="test",
                clone_url="https://example.invalid/rollback",
                default_branch="main",
            )
        )
        session.flush()
        raise RuntimeError("abort")
    with session_scope(factory) as session:
        assert (
            session.scalar(select(Repository).where(Repository.github_owner == "rollback")) is None
        )


@pytest.mark.parametrize(
    "owner,name", [("example", "sample"), ("EXAMPLE", "sample"), ("example", "SAMPLE")]
)
def test_repository_duplicates_rejected(
    factory: sessionmaker[Session],
    graph: dict[str, UUID],
    owner: str,
    name: str,
) -> None:
    with pytest.raises(IntegrityError) as error, session_scope(factory) as session:
        session.add(
            Repository(
                github_owner=owner,
                github_name=name,
                clone_url="https://example.invalid/duplicate",
                default_branch="main",
            )
        )
    assert getattr(error.value.orig, "sqlstate", None) == "23505"


def test_issue_uniqueness_is_repository_scoped(
    factory: sessionmaker[Session],
    graph: dict[str, UUID],
) -> None:
    with session_scope(factory) as session:
        other = Repository(
            github_owner="another-owner",
            github_name="sample",
            clone_url="https://example.invalid/other",
            default_branch="main",
        )
        session.add(other)
        session.flush()
        session.add(
            Issue(
                repository_id=other.id,
                github_issue_number=1,
                title="Different issue",
                source_url="https://example.invalid/other/issues/1",
            )
        )
    with pytest.raises(IntegrityError) as error, session_scope(factory) as session:
        session.add(
            Issue(
                repository_id=graph["repo"],
                github_issue_number=1,
                title="Duplicate issue",
                source_url="https://example.invalid/issues/1",
            )
        )
    assert getattr(error.value.orig, "sqlstate", None) == "23505"
    with session_scope(factory) as session:
        assert len(session.scalars(select(Issue).where(Issue.github_issue_number == 1)).all()) == 2


def test_foreign_keys_have_supporting_indexes(engine: Engine) -> None:
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        indexes = inspector.get_indexes(table.name)
        unique = inspector.get_unique_constraints(table.name)
        indexed_columns: list[Sequence[str | None]] = [index["column_names"] for index in indexes]
        indexed_columns.extend(constraint["column_names"] for constraint in unique)
        foreign_keys = inspector.get_foreign_keys(table.name)
        assert len(foreign_keys) == len(table.foreign_key_constraints)
        for foreign_key in foreign_keys:
            columns = foreign_key["constrained_columns"]
            assert any(index[: len(columns)] == columns for index in indexed_columns), table.name


def test_migration_downgrade_upgrade_and_metadata(engine: Engine) -> None:
    with engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.check(config)
        command.downgrade(config, "base")
        assert not (set(inspect(connection).get_table_names()) & set(Base.metadata.tables))
        command.upgrade(config, "head")
        assert set(Base.metadata.tables) <= set(inspect(connection).get_table_names())
        command.check(config)


def test_github_import_api_persists_and_deduplicates(
    factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    from contextlib import contextmanager

    import httpx
    from fastapi.testclient import TestClient

    from app.core.config import Settings
    from app.integrations.github.client import HttpGitHubClient
    from app.main import create_app
    from app.services.repositories import RepositoryService

    calls: list[str] = []
    title = "Original issue"
    failure_status: int | None = None
    failure_headers: dict[str, str] = {}
    token = "synthetic-audit-token-not-a-real-credential"
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.method == "GET"
        assert request.headers["Authorization"] == f"Bearer {token}"
        if failure_status is not None:
            return httpx.Response(failure_status, headers=failure_headers, json={"message": token})
        if "/issues/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "number": 7,
                    "title": title,
                    "body": None,
                    "state": "open",
                    "html_url": "https://github.invalid/owner/repo/issues/7",
                },
            )
        return httpx.Response(
            200,
            json={
                "owner": {"login": "Owner"},
                "name": "Repo",
                "clone_url": "https://github.invalid/Owner/Repo.git",
                "default_branch": "main",
            },
        )

    with httpx.Client(
        base_url="https://github.invalid/",
        headers={"Authorization": f"Bearer {token}"},
        transport=httpx.MockTransport(handler),
    ) as c:
        service = RepositoryService(factory, HttpGitHubClient(c))

        @contextmanager
        def resources(settings: Settings) -> Iterator[RepositoryService | None]:
            yield service

        settings = Settings(database_url=None, redis_url=None, celery_broker_url=None)
        with TestClient(create_app(settings, repository_factory=resources)) as api:
            response = api.post(
                "/repositories", json={"github_owner": "owner", "github_name": "repo"}
            )
            assert response.status_code == 200, response.text
            repo = response.json()
            assert repo["github_owner"] == "Owner"
            assert repo["github_name"] == "Repo"
            assert repo["default_branch"] == "main"
            again = api.post("/repositories", json={"github_owner": "OWNER", "github_name": "REPO"})
            assert again.json()["id"] == repo["id"]
            route = f"/repositories/{repo['id']}/issues/7/import"
            response = api.post(route)
            assert response.status_code == 200, response.text
            issue = response.json()
            assert issue["body"] is None
            assert issue["title"] == title and issue["github_issue_number"] == 7
            title = "Updated issue"
            response = api.post(route)
            assert response.json()["id"] == issue["id"]
            assert response.json()["title"] == title
            before = len(calls)
            assert api.get(f"/repositories/{repo['id']}").json() == again.json()
            assert api.get(f"/issues/{issue['id']}").json()["title"] == title
            assert len(calls) == before
            assert api.get(f"/issues/{uuid4()}").status_code == 404
            assert api.post(f"/repositories/{uuid4()}/issues/7/import").status_code == 404
            assert len(calls) == before
            cases: list[tuple[int, dict[str, str], int, str]] = [
                (401, {}, 401, "github_unauthorized"),
                (403, {}, 403, "github_forbidden"),
                (404, {}, 404, "github_not_found"),
                (403, {"x-ratelimit-remaining": "0"}, 429, "github_rate_limited"),
                (429, {"retry-after": "12"}, 429, "github_rate_limited"),
                (503, {}, 502, "github_response_error"),
            ]
            for status, headers, expected, code in cases:
                failure_status, failure_headers = status, headers
                for response in [
                    api.post(route),
                    api.post(
                        "/repositories", json={"github_owner": "owner", "github_name": "repo"}
                    ),
                ]:
                    assert response.status_code == expected
                    assert response.json() == {"detail": {"code": code}}
                    assert token not in response.text
                    if expected == 429:
                        assert int(response.headers["Retry-After"]) > 0
            assert api.get(f"/issues/{issue['id']}").json()["title"] == title
            assert token not in caplog.text
            assert "GitHub operation failed" in caplog.text
        with session_scope(factory) as session:
            assert len(list(session.scalars(select(Repository)))) == 1
            assert len(list(session.scalars(select(Issue)))) == 1


def test_workspace_synchronization_state(
    factory: sessionmaker[Session],
    tmp_path: Path,
    local_repository: Path,
) -> None:
    from app.integrations.git.runner import SubprocessGitRunner, WorkspaceError
    from app.services.workspace import WorkspaceService
    from app.services.workspace_sync import prepare_registered_repository

    with session_scope(factory) as session:
        repository = Repository(
            github_owner="local",
            github_name="fixture",
            clone_url=str(local_repository),
            default_branch="main",
            index_status="ready",
        )
        session.add(repository)
        session.flush()
        identifier = repository.id
    workspace = WorkspaceService(tmp_path / "workspaces", SubprocessGitRunner(), allow_local=True)
    result = prepare_registered_repository(identifier, factory, workspace)
    assert (result.path / "hello.txt").exists()
    with session_scope(factory) as session:
        saved = session.get(Repository, identifier)
        assert saved is not None
        assert saved.local_status == "ready" and saved.index_status == "pending"
        saved.default_branch = "missing-branch"
    with pytest.raises(WorkspaceError):
        prepare_registered_repository(identifier, factory, workspace)
    with session_scope(factory) as session:
        saved = session.get(Repository, identifier)
        assert saved is not None and saved.local_status == "error"
        saved.default_branch = "main"
    assert prepare_registered_repository(identifier, factory, workspace).commit == result.commit
    with pytest.raises(WorkspaceError, match="repository_not_registered"):
        prepare_registered_repository(uuid4(), factory, workspace)


@pytest.mark.skipif(os.environ.get("RUN_WORKER_TESTS") != "1", reason="Requires Redis worker tests")
def test_workspace_task_through_redis_worker(
    engine: Engine,
    tmp_path: Path,
    local_repository: Path,
) -> None:
    from functools import partial
    from unittest.mock import patch

    from celery.contrib.testing.worker import start_worker
    from kombu import Queue
    from pydantic import SecretStr

    from app.core.config import Settings
    from app.db.session import create_session_factory
    from app.integrations.git.runner import SubprocessGitRunner
    from app.services.workspace import WorkspaceService
    from app.workers.factory import create_celery_app
    from app.workers.workspaces import PREPARE_TASK_NAME

    settings = Settings(
        database_url=SecretStr(engine.url.render_as_string(hide_password=False)),
        workspace_root=tmp_path / "worker-workspaces",
        github_token=None,
    )
    sessions = create_session_factory(engine)
    with session_scope(sessions) as session:
        repository = Repository(
            github_owner="worker",
            github_name="local-fixture",
            clone_url=str(local_repository),
            default_branch="main",
        )
        session.add(repository)
        session.flush()
        identifier = repository.id
    app = create_celery_app(settings)
    queue = f"workspace-audit-{uuid4().hex}"
    app.conf.task_queues = (Queue(queue),)
    app.conf.task_default_queue = queue
    app.conf.task_routes = {PREPARE_TASK_NAME: {"queue": queue}}
    task_ids: list[str] = []
    try:
        # Only enable local remotes via the existing injection seam; real task, Git, DB and Redis.
        with (
            patch("app.workers.workspaces.Settings", return_value=settings),
            patch(
                "app.workers.workspaces.WorkspaceService",
                partial(WorkspaceService, allow_local=True),
            ),
            start_worker(app, pool="solo", perform_ping_check=False, queues=[queue]),
        ):
            results: list[dict[str, str]] = []
            for _ in range(2):
                task = app.send_task(PREPARE_TASK_NAME, args=[str(identifier)])
                task_ids.append(str(task.id))
                result = task.get(timeout=45)
                assert task.state == "SUCCESS"
                assert result["repository_id"] == str(identifier) and result["status"] == "ready"
                results.append(result)
            assert results[0] == results[1]
            checkout = settings.workspace_root / "repositories" / str(identifier)
            assert (checkout / "hello.txt").read_text() == "original\n"
            assert (
                SubprocessGitRunner().run(["rev-parse", "HEAD"], cwd=checkout)
                == results[0]["commit"]
            )
            with session_scope(sessions) as session:
                saved = session.get(Repository, identifier)
                assert saved is not None and saved.local_status == "ready"
                assert saved.index_status == "pending"
            print(
                f"Workspace worker evidence: task_ids={task_ids}, SUCCESS, "
                f"commit={results[0]['commit']}"
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
                saved = session.get(Repository, identifier)
                if saved is not None:
                    session.delete(saved)
