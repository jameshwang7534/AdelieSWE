"""API lifecycle and configuration checks requiring no external services."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.main import create_app
from app.services.readiness import (
    Checks,
    DependencyCheck,
    PostgresCheck,
    RedisCheck,
    readiness_resources,
)


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name, raising=False)


def test_settings_defaults_and_optional_secrets() -> None:
    settings = Settings()
    assert settings.app_env == "development"
    assert settings.github_token is None
    assert settings.llm_api_key is None
    assert settings.database_url is None
    assert settings.redis_url is None
    assert settings.embedding_dim == 1536


def test_settings_dotenv_environment_precedence_and_redaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "APP_NAME=From file\nPOSTGRES_DB=ignored\nLLM_API_KEY=\nEMBEDDING_MODEL=\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_NAME", "From environment")
    monkeypatch.setenv("EMBEDDING_DIM", "768")
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-test-value")
    settings = Settings()
    assert settings.app_name == "From environment"
    assert settings.embedding_dim == 768
    assert settings.llm_api_key is None
    assert settings.embedding_model is None
    assert "synthetic-test-value" not in repr(settings)
    assert "synthetic-test-value" not in settings.model_dump_json()


@pytest.mark.parametrize(
    ("name", "value"),
    [("EMBEDDING_DIM", "0"), ("LOG_LEVEL", "invalid"), ("DATABASE_URL", "sqlite:///test")],
)
def test_settings_reject_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


def test_startup_liveness_readiness_and_shutdown() -> None:
    postgres = Mock(spec=DependencyCheck)
    redis = Mock(spec=DependencyCheck)
    events: list[str] = []

    @contextmanager
    def resources(settings: Settings) -> Iterator[Checks]:
        events.append("startup")
        try:
            yield Checks(postgres, redis)
        finally:
            events.append("shutdown")

    application = create_app(Settings(app_name="Test API"), resources)
    assert events == []
    with TestClient(application) as client:
        assert events == ["startup"]
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        postgres.check.assert_not_called()
        redis.check.assert_not_called()
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ready",
            "dependencies": {"postgres": {"status": "ok"}, "redis": {"status": "ok"}},
        }
        assert client.get("/openapi.json").json()["info"]["title"] == "Test API"
        assert client.get("/docs").status_code == 200
    assert events == ["startup", "shutdown"]
    assert not hasattr(application.state, "checks")


@pytest.mark.parametrize("failed_dependency", ["postgres", "redis"])
def test_dependency_failure_and_recovery(
    failed_dependency: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    probes = {name: Mock(spec=DependencyCheck) for name in ("postgres", "redis")}
    probes[failed_dependency].check.side_effect = TimeoutError("synthetic-sensitive-detail")

    @contextmanager
    def resources(settings: Settings) -> Iterator[Checks]:
        yield Checks(probes["postgres"], probes["redis"])

    with TestClient(create_app(Settings(), resources)) as client:
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
        for name, probe in probes.items():
            expected = "error" if name == failed_dependency else "ok"
            assert response.json()["dependencies"][name]["status"] == expected
            probe.check.assert_called_once()
        assert "synthetic-sensitive-detail" not in response.text + caplog.text
        assert f"Readiness check failed: {failed_dependency}" in caplog.text
        assert client.get("/health").status_code == 200
        probes[failed_dependency].check.side_effect = None
        assert client.get("/ready").status_code == 200


def test_unconfigured_dependencies_do_not_prevent_startup() -> None:
    with TestClient(create_app(Settings())) as client:
        assert client.get("/health").status_code == 200
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["dependencies"] == {
            "postgres": {"status": "unconfigured"},
            "redis": {"status": "unconfigured"},
        }


def test_real_adapter_probes_and_cleanup_without_network() -> None:
    settings = Settings(
        database_url=SecretStr("postgresql+psycopg://localhost/example"),
        redis_url=SecretStr("redis://localhost:6379/0"),
    )
    with (
        patch("app.services.readiness.create_engine") as engine_factory,
        patch("app.services.readiness.Redis.from_url") as redis_factory,
    ):
        engine = engine_factory.return_value
        connection = engine.connect.return_value.__enter__.return_value
        connection.execute.return_value.scalar_one.return_value = 1
        with readiness_resources(settings) as checks:
            assert isinstance(checks.postgres, PostgresCheck)
            assert isinstance(checks.redis, RedisCheck)
            engine.connect.assert_not_called()
            checks.postgres.check()
            checks.redis.check()
            assert str(connection.execute.call_args.args[0]) == "SELECT 1"
            redis_factory.return_value.ping.assert_called_once()
        engine.dispose.assert_called_once()
        redis_factory.return_value.close.assert_called_once()


def test_partial_startup_failure_disposes_engine() -> None:
    settings = Settings(
        database_url=SecretStr("postgresql+psycopg://localhost/example"),
        redis_url=SecretStr("redis://localhost:6379/0"),
    )
    with (
        patch("app.services.readiness.create_engine") as engine_factory,
        patch("app.services.readiness.Redis.from_url", side_effect=RuntimeError("startup failed")),
    ):
        with pytest.raises(RuntimeError, match="startup failed"), TestClient(create_app(settings)):
            pytest.fail("Startup should have failed")
        engine_factory.return_value.dispose.assert_called_once()
