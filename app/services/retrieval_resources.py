"""Lifespan ownership for the lazy retrieval database engine."""

from collections.abc import Iterator
from contextlib import contextmanager

from app.core.config import Settings
from app.db.session import create_database_engine, create_session_factory
from app.retrieval.base import Retriever
from app.retrieval.bm25 import BM25Retriever
from app.retrieval.postgres import PostgresChunkSource


@contextmanager
def retrieval_resources(settings: Settings) -> Iterator[Retriever | None]:
    if settings.database_url is None:
        yield None
        return
    engine = create_database_engine(settings)
    try:
        yield BM25Retriever(PostgresChunkSource(create_session_factory(engine)))
    finally:
        engine.dispose()
