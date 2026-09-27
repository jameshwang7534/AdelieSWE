"""Plan read/write resources independent of model credentials."""

from collections.abc import Iterator
from contextlib import contextmanager

from app.core.config import Settings
from app.db.session import create_database_engine, create_session_factory
from app.services.plans import PlanService


@contextmanager
def plan_resources(settings: Settings) -> Iterator[PlanService | None]:
    if settings.database_url is None:
        yield None
        return
    engine = create_database_engine(settings)
    try:
        yield PlanService(create_session_factory(engine))
    finally:
        engine.dispose()
