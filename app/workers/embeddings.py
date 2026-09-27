"""Explicit asynchronous embedding generation, separate from source indexing."""

import logging
from uuid import UUID

from app.core.config import Settings

EMBED_TASK_NAME = "repository.embed_code"
logger = logging.getLogger(__name__)


def embed_code(repository_id: str) -> dict[str, str | int]:
    from app.db.session import create_database_engine, create_session_factory
    from app.indexing.embeddings import EmbeddingService
    from app.integrations.git.runner import SubprocessGitRunner
    from app.integrations.llm.embeddings import EmbeddingError, EmbeddingUnconfigured
    from app.services.embedding_resources import embedding_resources
    from app.services.workspace import WorkspaceService

    try:
        settings = Settings()
        with embedding_resources(settings) as provider:
            if provider is None:
                raise EmbeddingUnconfigured(
                    "embedding_unconfigured: set LLM_API_KEY and EMBEDDING_MODEL"
                )
            engine = create_database_engine(settings)
            try:
                result = EmbeddingService(
                    create_session_factory(engine),
                    WorkspaceService(settings.workspace_root, SubprocessGitRunner()),
                    provider,
                    settings.embedding_batch_size,
                ).generate(UUID(repository_id))
            finally:
                engine.dispose()
        logger.info("Embedding generation complete repository_id=%s", repository_id)
        return result
    except EmbeddingUnconfigured:
        logger.warning("Embedding generation requires LLM_API_KEY and EMBEDDING_MODEL")
        raise
    except Exception:
        logger.warning("Embedding generation failed")
        raise EmbeddingError("embedding_generation_failed") from None
