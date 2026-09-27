"""Lazy database resources for issue-context reads."""

from collections.abc import Iterator
from contextlib import contextmanager

from app.core.config import Settings
from app.db.session import create_database_engine, create_session_factory
from app.services.context_store import ContextStore, PostgresContextStore


@contextmanager
def context_resources(settings: Settings) -> Iterator[ContextStore | None]:
    if settings.database_url is None:
        yield None
        return
    engine = create_database_engine(settings)
    try:
        yield PostgresContextStore(create_session_factory(engine))
    finally:
        engine.dispose()
