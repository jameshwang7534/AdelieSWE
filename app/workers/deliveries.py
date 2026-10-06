"""Durable wrapper for standalone repository workers, with no connections on import."""

from collections.abc import Callable, Mapping
from uuid import UUID

from celery import Task

from app.core.config import Settings
from app.integrations.llm.embeddings import EmbeddingUnconfigured


def repository_delivery(task: Task, repository_id: str) -> dict[str, object]:
    from app.db.session import create_database_engine, create_session_factory
    from app.orchestration.deliveries import DeliveryRecords
    from app.workers.embeddings import EMBED_TASK_NAME, embed_code
    from app.workers.indexing import INDEX_TASK_NAME, index_code
    from app.workers.workspaces import PREPARE_TASK_NAME, prepare_workspace

    operations: dict[str, Callable[[str], Mapping[str, object]]] = {
        EMBED_TASK_NAME: embed_code,
        INDEX_TASK_NAME: index_code,
        PREPARE_TASK_NAME: prepare_workspace,
    }
    engine = create_database_engine(Settings())
    try:
        return DeliveryRecords(create_session_factory(engine)).execute(
            UUID(str(task.request.id)),
            str(task.name),
            UUID(repository_id),
            lambda: dict(operations[task.name](repository_id)),
        )
    except EmbeddingUnconfigured:
        raise EmbeddingUnconfigured(
            "embedding_unconfigured: set LLM_API_KEY and EMBEDDING_MODEL"
        ) from None
    except Exception:
        raise RuntimeError("repository_delivery_failed") from None
    finally:
        engine.dispose()
