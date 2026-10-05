"""Celery stage execution and pending-row recovery. Database connections are invocation-local."""

import asyncio
import logging
from uuid import UUID

ADVANCE_WORKFLOW = "workflow.advance"
RECOVER_WORKFLOWS = "workflow.recover"
logger = logging.getLogger(__name__)


def advance_workflow(workflow_id: str, stage: str) -> dict[str, object]:
    from app.core.config import Settings
    from app.db.session import create_database_engine, create_session_factory
    from app.orchestration.workflow_resources import workflow_runtime
    from app.services.workflow_resources import CeleryWorkflowQueue
    from app.workers.factory import create_celery_app

    settings = Settings()
    engine = create_database_engine(settings)
    application = create_celery_app(settings)
    try:

        async def run() -> dict[str, object]:
            async with workflow_runtime(settings, create_session_factory(engine)) as runtime:
                result = await runtime.advance(UUID(workflow_id), expected_stage=stage)
            if runtime.did_advance and result.status == "pending":
                try:
                    CeleryWorkflowQueue(application).enqueue(result)
                except Exception:
                    logger.warning(
                        "Workflow wake-up failed; pending checkpoint retained id=%s", result.id
                    )
            return result.model_dump(mode="json")

        return asyncio.run(run())
    except Exception:
        raise RuntimeError("workflow_worker_failed") from None
    finally:
        application.close()
        engine.dispose()


def recover_workflows() -> dict[str, int]:
    from app.core.config import Settings
    from app.services.workflow_resources import workflow_api_resources

    count = 0
    with workflow_api_resources(Settings()) as resources:
        if resources is None:
            raise RuntimeError("workflow_unconfigured")
        records, queue = resources
        for identifier in records.recoverable():
            queue.enqueue(records.get(identifier))
            count += 1
    return {"dispatched": count}
