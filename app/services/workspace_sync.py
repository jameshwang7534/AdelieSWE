"""Persist synchronization state without implementing indexing."""

from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope
from app.integrations.git.runner import WorkspaceError
from app.models import Repository
from app.services.workspace import WorkspaceResult, WorkspaceService


def prepare_registered_repository(
    repository_id: UUID,
    sessions: sessionmaker[Session],
    workspace: WorkspaceService,
) -> WorkspaceResult:
    with session_scope(sessions) as session:
        repository = session.get(Repository, repository_id)
        if repository is None:
            raise WorkspaceError("repository_not_registered")
        source, branch = repository.clone_url, repository.default_branch

    def update_status(status: str) -> None:
        with session_scope(sessions) as session:
            record = session.get(Repository, repository_id)
            if record is None:
                raise WorkspaceError("repository_not_registered")
            record.local_status = status
            # Existing code context must not be considered current during/after a new sync.
            record.index_status = "pending"

    return workspace.prepare(repository_id, source, branch, on_status=update_status)
