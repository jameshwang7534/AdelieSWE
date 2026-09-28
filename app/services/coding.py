"""Explicit coding invocation; no test execution, auto-debugging, or completion claim."""

import asyncio
import json
from uuid import UUID

from app.agents.coding import CodingAgent
from app.core.config import Settings
from app.integrations.llm.provider import LLMError, TokenUsage
from app.sandbox.base import SandboxError
from app.schemas.coding import CodeChangeProposal
from app.schemas.context import IssueContext
from app.services.code_patches import CodePatchService
from app.services.coding_records import CodingRecords
from app.services.patch_format import PatchError


class CodingError(Exception):
    """Sanitized coding failure; detailed bounded errors remain on AgentRun."""


class CodingService:
    def __init__(
        self,
        settings: Settings,
        agent: CodingAgent,
        records: CodingRecords,
        patches: CodePatchService,
    ) -> None:
        self.settings, self.agent, self.records, self.patches = settings, agent, records, patches

    def redact(self, text: str) -> str:
        for secret in (
            self.settings.github_token,
            self.settings.llm_api_key,
            self.settings.database_url,
            self.settings.redis_url,
        ):
            if secret and secret.get_secret_value():
                text = text.replace(secret.get_secret_value(), "[REDACTED]")
        return text

    async def run(self, run_id: UUID, task_id: UUID, context: IssueContext) -> CodeChangeProposal:
        agent_id, inputs = self.records.start(run_id, task_id, context, self.settings.llm_model)
        workspace = (
            self.settings.workspace_root / "executions" / str(context.repository.id) / str(run_id)
        )
        usage: TokenUsage | None = None
        try:
            with self.patches.locked(workspace) as safe:
                inputs.workspace_status = self.patches.status(safe)
                serialized = inputs.model_dump_json()
                if len(serialized) > 100000 or self.redact(serialized) != serialized:
                    raise PatchError("unsafe_or_oversized_coding_input")
                generated = await self.agent.propose(inputs)
                usage = generated.usage
                proposal = generated.output
                serialized_proposal = proposal.model_dump_json()
                if self.redact(serialized_proposal) != serialized_proposal:
                    raise PatchError("secret_in_code_proposal")
                diff = self.patches.apply(safe, proposal)
                self.records.finish(
                    run_id,
                    task_id,
                    agent_id,
                    success=True,
                    metadata={
                        "proposal": json.loads(serialized_proposal),
                        "git_diff": self.redact(diff),
                        "validation": "not_run",
                    },
                    usage=usage,
                )
                return proposal
        except asyncio.CancelledError:
            self.records.finish(
                run_id,
                task_id,
                agent_id,
                success=False,
                metadata={"error_code": "coding_cancelled"},
                usage=usage,
            )
            raise
        except Exception as error:
            code = error.code if isinstance(error, (PatchError, LLMError)) else "coding_failed"
            if isinstance(error, SandboxError):
                code = str(error)
            diagnostic = self.redact(error.diagnostic) if isinstance(error, PatchError) else ""
            self.records.finish(
                run_id,
                task_id,
                agent_id,
                success=False,
                metadata={
                    "error_code": code,
                    "diagnostic": diagnostic,
                    "workspace_may_be_modified": True,
                },
                usage=usage,
            )
            raise CodingError(code) from None
