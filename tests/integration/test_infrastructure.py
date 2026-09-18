"""Opt-in checks against the already-running development Compose services."""

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INFRASTRUCTURE_TESTS") != "1",
    reason="Start Docker Compose and set RUN_INFRASTRUCTURE_TESTS=1 to run infrastructure checks.",
)
ROOT = Path(__file__).resolve().parents[2]


def compose_output(*args: str) -> str:
    """Run a read-only Compose check and surface failures or timeouts."""
    result = subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def postgres_query(sql: str) -> str:
    """Use the container's configured credentials without printing them."""
    return compose_output(
        "exec",
        "-T",
        "postgres",
        "sh",
        "-c",
        'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 '
        '-U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -Atc "$1"',
        "sh",
        sql,
    )


def test_postgres_16_and_vector_extension() -> None:
    assert postgres_query("SELECT current_setting('server_version_num')::int / 10000") == "16"
    assert postgres_query("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    assert postgres_query("SELECT '[1,2,3]'::vector <-> '[1,2,3]'::vector") == "0"


def test_redis_health_and_persistence_configuration() -> None:
    assert compose_output("exec", "-T", "redis", "redis-cli", "ping") == "PONG"
    assert compose_output(
        "exec", "-T", "redis", "redis-cli", "--raw", "CONFIG", "GET", "appendonly"
    ).splitlines() == ["appendonly", "yes"]
    assert compose_output(
        "exec", "-T", "redis", "redis-cli", "--raw", "CONFIG", "GET", "appendfsync"
    ).splitlines() == ["appendfsync", "everysec"]
