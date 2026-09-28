"""Lifespan-owned lazy database resources for orchestration."""

from collections.abc import Iterator
from contextlib import contextmanager

from app.core.config import Settings
from app.db.session import create_database_engine, create_session_factory
from app.services.executions import ExecutionService


@contextmanager
def execution_resources(settings: Settings) -> Iterator[ExecutionService | None]:
    if settings.database_url is None:
        yield None
        return
    engine = create_database_engine(settings)
    try:
        yield ExecutionService(create_session_factory(engine))
    finally:
        engine.dispose()
