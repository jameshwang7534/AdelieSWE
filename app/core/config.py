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
    github_git_host: str = "github.com"
    git_commit_name: str = Field(default="AI Software Engineering Platform", min_length=1)
    git_commit_email: str = Field(default="ai-platform@example.invalid", min_length=1)
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_timeout_seconds: float = Field(default=30, ge=1, le=300)
    llm_max_retries: int = Field(default=3, ge=0, le=5)
    llm_max_output_tokens: int = Field(default=2048, ge=1, le=32768)
    planner_validation_retries: int = Field(default=2, ge=0, le=3)
    max_recovery_attempts: int = Field(default=3, ge=0, le=10)
    review_max_input_chars: int = Field(default=200000, ge=1000, le=2000000)
    orchestration_recovery_seconds: int = Field(default=30, ge=5, le=3600)
    workflow_required_tests: tuple[str, ...] = Field(
        default=("python -m unittest",), min_length=1, max_length=8
    )
    workflow_stage_attempts: int = Field(default=3, ge=1, le=10)
    embedding_model: str | None = None
    embedding_dim: int = Field(default=1536, gt=0)
    embedding_batch_size: int = Field(default=16, ge=1, le=128)
    embedding_max_retries: int = Field(default=3, ge=0, le=5)
    embedding_timeout_seconds: int = Field(default=30, ge=1, le=120)
    embedding_send_dimensions: bool = True
    dependency_timeout_seconds: int = Field(default=2, ge=1, le=30)
    workspace_root: Path = Path("workspaces")
    sandbox_images: dict[str, str] = Field(default_factory=lambda: {"python": "python:3.12-slim"})
    sandbox_timeout_seconds: int = Field(default=60, ge=1, le=900)
    sandbox_cpu_limit: float = Field(default=1, ge=0.1, le=4)
    sandbox_memory_mb: int = Field(default=256, ge=64, le=2048)
    sandbox_pid_limit: int = Field(default=64, ge=16, le=256)
    sandbox_output_bytes: int = Field(default=1048576, ge=1024, le=4194304)
    sandbox_user: str = Field(default="1000:1000", pattern=r"^[1-9][0-9]{0,8}:[1-9][0-9]{0,8}$")
    test_allowed_commands: tuple[str, ...] = (
        "pytest",
        "python -m pytest",
        "python -m unittest",
        "npm test",
        "npm run test",
        "npm run lint",
        "pnpm test",
        "yarn test",
        "go test",
        "cargo test",
    )
    index_max_file_bytes: int = Field(default=262144, ge=1, le=10485760)
    index_chunk_max_lines: int = Field(default=120, ge=1, le=1000)
    index_chunk_max_chars: int = Field(default=8000, ge=1, le=100000)
    context_max_issue_chars: int = Field(default=12000, ge=512, le=50000)
    context_max_code_chars: int = Field(default=24000, ge=1, le=100000)
    context_max_chunks: int = Field(default=12, ge=1, le=50)
    context_max_files: int = Field(default=8, ge=1, le=50)
    context_max_queries: int = Field(default=3, ge=1, le=5)
    context_query_chars: int = Field(default=1000, ge=32, le=2000)
    context_include_neighbors: bool = True

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
