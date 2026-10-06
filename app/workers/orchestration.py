"""Celery reconciles persisted state only; it never invokes an executor."""

import logging
from uuid import UUID

RECONCILE_TASK_NAME = "execution.reconcile"
RECOVER_TASK_NAME = "execution.recover"
logger = logging.getLogger(__name__)


def reconcile_execution(execution_id: str) -> dict[str, object]:
    from app.core.config import Settings
    from app.services.execution_resources import execution_resources

    try:
        with execution_resources(Settings()) as service:
            if service is None:
                raise RuntimeError("database_unconfigured")
            result = service.reconcile(UUID(execution_id))
            return result.model_dump(mode="json")
    except Exception:
        logger.warning("Execution reconciliation failed")
        raise RuntimeError("execution_reconciliation_failed") from None


def recover_executions() -> dict[str, int]:
    from app.core.config import Settings
    from app.services.execution_resources import execution_resources

    count = 0
    try:
        with execution_resources(Settings()) as service:
            if service is None:
                raise RuntimeError("database_unconfigured")
            service.recover_stale(Settings().execution_stale_seconds)
            after = None
            while identifiers := service.active_ids(after):
                for identifier in identifiers:
                    service.reconcile(identifier)
                    count += 1
                after = identifiers[-1]
        return {"reconciled": count}
    except Exception:
        logger.warning("Execution recovery failed")
        raise RuntimeError("execution_recovery_failed") from None
