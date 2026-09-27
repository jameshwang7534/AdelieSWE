"""Asynchronous CodeChunk persistence; no indexing work during worker import."""

import logging
from uuid import UUID

from app.core.config import Settings

INDEX_TASK_NAME = "repository.index_code"
logger = logging.getLogger(__name__)


def index_code(repository_id: str) -> dict[str, str | int]:
    from app.db.session import create_database_engine, create_session_factory
    from app.indexing.scanner import RepositoryScanner
    from app.indexing.service import IndexingError, IndexingService
    from app.integrations.git.runner import SubprocessGitRunner
    from app.services.workspace import WorkspaceService

    try:
        identifier = UUID(repository_id)
        settings = Settings()
        engine = create_database_engine(settings)
        try:
            service = IndexingService(
                create_session_factory(engine),
                WorkspaceService(settings.workspace_root, SubprocessGitRunner()),
                RepositoryScanner(settings.index_max_file_bytes, settings.index_chunk_max_chars),
                max_lines=settings.index_chunk_max_lines,
                max_chars=settings.index_chunk_max_chars,
            )
            result = service.index(identifier)
        finally:
            engine.dispose()
        logger.info("Repository indexing complete repository_id=%s", identifier)
        return result
    except Exception:
        logger.warning("Repository indexing failed")
        raise IndexingError("repository_indexing_failed") from None
