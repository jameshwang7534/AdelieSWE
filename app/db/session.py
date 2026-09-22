"""Explicit engine ownership and short-lived transactional sessions."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings


def create_database_engine(settings: Settings) -> Engine:
    """Construct a lazy engine; the caller must dispose it at shutdown."""
    if settings.database_url is None:
        raise ValueError("DATABASE_URL is required for database operations")
    return create_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_timeout=settings.dependency_timeout_seconds,
        hide_parameters=True,
        connect_args={"connect_timeout": settings.dependency_timeout_seconds},
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit on success, rollback on errors, always close; do not share sessions."""
    with factory.begin() as session:
        yield session
