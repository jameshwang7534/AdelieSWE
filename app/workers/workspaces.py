"""Workspace task entry point; database and filesystem resources are created only on execution."""

import logging
from urllib.parse import urlsplit
from uuid import UUID

from app.core.config import Settings
from app.integrations.git.runner import SubprocessGitRunner, WorkspaceError
from app.services.workspace import WorkspaceService

PREPARE_TASK_NAME = "repository.prepare_workspace"
logger = logging.getLogger(__name__)


def prepare_workspace(repository_id: str) -> dict[str, str]:
    # Local imports keep worker discovery free of database initialization.
    from app.db.session import create_database_engine, create_session_factory
    from app.services.workspace_sync import prepare_registered_repository

    try:
        identifier = UUID(repository_id)
        settings = Settings()
        host = urlsplit(settings.github_api_url).hostname
        workspace = WorkspaceService(
            settings.workspace_root,
            SubprocessGitRunner(),
            github_host="github.com" if host == "api.github.com" else str(host),
            token=settings.github_token,
        )
        engine = create_database_engine(settings)
        try:
            result = prepare_registered_repository(
                identifier, create_session_factory(engine), workspace
            )
        finally:
            engine.dispose()
        logger.info("Repository workspace ready repository_id=%s", identifier)
        return {"repository_id": str(identifier), "status": "ready", "commit": result.commit}
    except Exception:
        logger.warning("Repository workspace preparation failed")
        raise WorkspaceError("workspace_preparation_failed") from None
