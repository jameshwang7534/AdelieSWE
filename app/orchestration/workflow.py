"""Execute one claimed stage; duplicate deliveries cannot repeat in-flight work."""

from typing import Protocol
from uuid import UUID

from app.orchestration.failures import SAFE_RETRY_STAGES, provider_failure
from app.orchestration.workflow_records import WorkflowClaim, WorkflowError, WorkflowRecords
from app.orchestration.workflow_stages import StageResult
from app.schemas.workflows import WorkflowStatus


class StageRunner(Protocol):
    async def run(self, claim: WorkflowClaim) -> StageResult: ...


class WorkflowEngine:
    def __init__(self, records: WorkflowRecords, stages: StageRunner) -> None:
        self.records, self.stages = records, stages
        self.did_advance = False

    async def advance(
        self,
        identifier: UUID,
        expected_stage: str | None = None,
        expected_generation: int | None = None,
    ) -> WorkflowStatus:
        self.did_advance = False
        claim = self.records.claim(identifier, expected_stage, expected_generation)
        if claim is None:
            return self.records.get(identifier)
        self.did_advance = True
        try:
            result = await self.stages.run(claim)
        except Exception as error:
            # Expose only fixed workflow/provider codes, never raw external exceptions.
            code, retryable, minimum_delay = provider_failure(error)
            if isinstance(error, WorkflowError):
                code = str(error)
            blocked = claim.stage in {"implement", "review"} or isinstance(error, WorkflowError)
            if code in {"llm_unconfigured", "workflow_embeddings_unconfigured"}:
                blocked = False
            delay = None
            if retryable and claim.stage in SAFE_RETRY_STAGES and not blocked:
                attempts = self.records.get(identifier).attempts
                delay = max(
                    minimum_delay,
                    min(3600, self.records.settings.workflow_retry_seconds * 2 ** (attempts - 1)),
                )
            self.records.finish(claim, {}, error=code, blocked=blocked, retry_seconds=delay)
        else:
            self.records.finish(claim, result.data, repeat=result.repeat)
        return self.records.get(identifier)
