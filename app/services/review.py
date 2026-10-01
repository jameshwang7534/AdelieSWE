"""Read-only accumulated diff review with independent mechanical gates and no PR creation."""

import asyncio
from hashlib import sha256
from uuid import UUID

from app.agents.review import ReviewAgent
from app.core.config import Settings
from app.integrations.llm.provider import LLMError, TokenUsage
from app.schemas.context import IssueContext
from app.schemas.review import ReviewDecision
from app.schemas.testing import RepositoryTestConfig
from app.services.code_patches import CodePatchService
from app.services.review_records import ReviewRecords


class ReviewError(Exception):
    """Safe operational failure; never includes raw provider content or credentials."""


class ReviewService:
    def __init__(
        self,
        settings: Settings,
        agent: ReviewAgent,
        records: ReviewRecords,
        patches: CodePatchService,
    ) -> None:
        self.settings, self.agent, self.records, self.patches = settings, agent, records, patches

    def _contains_secret(self, text: str) -> bool:
        return any(
            secret and secret.get_secret_value() and secret.get_secret_value() in text
            for secret in (
                self.settings.github_token,
                self.settings.llm_api_key,
                self.settings.database_url,
                self.settings.redis_url,
            )
        )

    async def run(
        self, run_id: UUID, context: IssueContext, required: RepositoryTestConfig
    ) -> ReviewDecision:
        agent_id, snapshot = self.records.start(run_id, context, required, self.settings.llm_model)
        decision = ReviewDecision(
            review=None, mechanical_errors=tuple(snapshot.errors), approved=False, diff_hash=None
        )
        usage: TokenUsage | None = None
        try:
            if snapshot.errors:
                return self.records.finish(
                    run_id, agent_id, context, required, snapshot, decision, usage
                )
            workspace = (
                self.settings.workspace_root
                / "executions"
                / str(context.repository.id)
                / str(run_id)
            )
            with self.patches.locked(workspace) as safe:
                diff = self.patches.current_diff(safe)
                decision.diff_hash = sha256(diff.encode()).hexdigest()
                errors: list[str] = []
                if decision.diff_hash != snapshot.tested_diff_hash:
                    errors.append("workspace_changed_since_tests")
                if not diff:
                    errors.append("no_changes_to_review")
                if any(
                    name not in self.settings.test_allowed_commands
                    for name in required.required_commands
                ):
                    errors.append("required_command_disabled")
                snapshot.inputs.full_diff = diff
                serialized = snapshot.inputs.model_dump_json()
                if len(serialized) > self.settings.review_max_input_chars:
                    errors.append("review_input_too_large")
                if self._contains_secret(serialized):
                    errors.append("secret_in_review_input")
                if not errors:
                    async with asyncio.timeout(self.settings.llm_timeout_seconds):
                        result = await self.agent.review(snapshot.inputs)
                    usage = result.usage
                    if self._contains_secret(result.output.model_dump_json()):
                        raise ReviewError("secret_in_review_output")
                    decision.review = result.output
                    if self.patches.current_diff(safe) != diff:
                        errors.append("workspace_changed_during_review")
                decision.mechanical_errors = tuple(errors)
                decision.approved = (
                    not errors
                    and decision.review is not None
                    and decision.review.approved
                    and not any(f.severity == "blocking" for f in decision.review.findings)
                )
                return self.records.finish(
                    run_id, agent_id, context, required, snapshot, decision, usage
                )
        except asyncio.CancelledError:
            self.records.finish(
                run_id, agent_id, context, required, snapshot, decision, usage, "review_cancelled"
            )
            raise
        except Exception as error:
            code = error.code if isinstance(error, LLMError) else "review_operation_failed"
            if isinstance(error, TimeoutError):
                code = "llm_timeout"
            self.records.finish(
                run_id, agent_id, context, required, snapshot, decision, usage, code
            )
            raise ReviewError(code) from None
