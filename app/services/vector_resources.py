"""Optional vector retrieval clients with lifespan cleanup."""

from collections.abc import Iterator
from contextlib import contextmanager

from app.core.config import Settings
from app.db.session import create_database_engine, create_session_factory
from app.retrieval.vector import VectorRetriever
from app.services.embedding_resources import embedding_resources


@contextmanager
def vector_resources(settings: Settings) -> Iterator[VectorRetriever | None]:
    if settings.database_url is None:
        yield None
        return
    with embedding_resources(settings) as provider:
        if provider is None:
            yield None
            return
        engine = create_database_engine(settings)
        try:
            yield VectorRetriever(create_session_factory(engine), provider)
        finally:
            engine.dispose()
