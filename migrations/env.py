"""Alembic entry point using centralized settings or an injected test connection."""

from alembic import context
from sqlalchemy import Connection

import app.models  # noqa: F401 -- registers mapped tables
from app.core.config import Settings
from app.db.base import Base
from app.db.session import create_database_engine


def run_on_connection(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations() -> None:
    if context.is_offline_mode():
        settings = Settings()
        if settings.database_url is None:
            raise ValueError("DATABASE_URL is required for migrations")
        context.configure(
            url=settings.database_url.get_secret_value(),
            target_metadata=Base.metadata,
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
        )
        with context.begin_transaction():
            context.run_migrations()
    elif isinstance(connection := context.config.attributes.get("connection"), Connection):
        run_on_connection(connection)
    else:
        engine = create_database_engine(Settings())
        try:
            with engine.connect() as connection:
                run_on_connection(connection)
        finally:
            engine.dispose()


run_migrations()
