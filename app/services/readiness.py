"""Mockable, bounded readiness probes and their resource lifetime."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Protocol

from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from sqlalchemy import Engine, create_engine, text

from app.core.config import Settings


class DependencyCheck(Protocol):
    """A dependency probe raises on failure and never returns sensitive details."""

    def check(self) -> None: ...


@dataclass(frozen=True)
class Checks:
    postgres: DependencyCheck | None
    redis: DependencyCheck | None


@dataclass
class PostgresCheck:
    engine: Engine

    def check(self) -> None:
        with self.engine.connect() as connection:
            if connection.execute(text("SELECT 1")).scalar_one() != 1:
                raise RuntimeError("Unexpected database probe result")


@dataclass
class RedisCheck:
    client: Redis

    def check(self) -> None:
        if not self.client.ping():
            raise RuntimeError("Redis did not acknowledge the probe")


@contextmanager
def readiness_resources(settings: Settings) -> Iterator[Checks]:
    """Construct lazy clients and release even partially initialized resources."""
    with ExitStack() as stack:
        postgres = None
        redis = None
        timeout = settings.dependency_timeout_seconds
        if settings.database_url is not None:
            engine = create_engine(
                settings.database_url.get_secret_value(),
                pool_timeout=timeout,
                connect_args={
                    "connect_timeout": timeout,
                    "options": f"-c statement_timeout={timeout * 1000}",
                },
            )
            stack.callback(engine.dispose)
            postgres = PostgresCheck(engine)
        if settings.redis_url is not None:
            client = Redis.from_url(
                settings.redis_url.get_secret_value(),
                socket_connect_timeout=timeout,
                socket_timeout=timeout,
                retry=Retry(NoBackoff(), 0),
            )
            stack.callback(client.close)
            redis = RedisCheck(client)
        yield Checks(postgres=postgres, redis=redis)
