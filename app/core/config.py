"""Centralized environment configuration without import-time side effects."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Load case-insensitive environment names; empty placeholders use defaults."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_env: str = "development"
    app_name: str = Field(default="AI Software Engineering Platform", min_length=1)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_url: SecretStr | None = None
    redis_url: SecretStr | None = None
    celery_broker_url: SecretStr | None = None
    celery_result_backend: SecretStr | None = None
    github_token: SecretStr | None = None
    github_api_url: str = "https://api.github.com"
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    embedding_model: str | None = None
    embedding_dim: int = Field(default=1536, gt=0)
    dependency_timeout_seconds: int = Field(default=2, ge=1, le=30)
    workspace_root: Path = Path("workspaces")

    @field_validator("github_api_url")
    @classmethod
    def validate_github_api_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "GITHUB_API_URL must be an HTTPS base URL without credentials or query"
            )
        return value

    @field_validator("database_url", "redis_url", "celery_broker_url", "celery_result_backend")
    @classmethod
    def validate_connection_url(cls, value: SecretStr | None) -> SecretStr | None:
        """Validate connection schemes separately below; never echo credentials."""
        if value is not None and not value.get_secret_value().strip():
            return None
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            parsed = urlsplit(value.get_secret_value())
            if parsed.scheme != "postgresql+psycopg" or not parsed.hostname:
                raise ValueError("DATABASE_URL must use postgresql+psycopg with a hostname")
        return value

    @field_validator("redis_url", "celery_broker_url", "celery_result_backend")
    @classmethod
    def validate_redis_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            parsed = urlsplit(value.get_secret_value())
            if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
                raise ValueError("Redis connection URLs must use redis or rediss with a hostname")
        return value
