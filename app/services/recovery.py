"""Bounded coding -> testing -> debugging. Each repair consumes a persisted attempt."""

import asyncio
from hashlib import sha256
from uuid import UUID

from app.agents.debugging import DebugAgent
from app.core.config import Settings
from app.integrations.llm.provider import LLMError, TokenUsage
from app.schemas.context import IssueContext
from app.schemas.debugging import DebugInput, RecoveryReport
from app.schemas.planning import PlanTaskProposal
from app.schemas.testing import RepositoryTestConfig, TestReport
from app.services.coding import CodingService
from app.services.patch_format import PatchError
from app.services.recovery_records import RecoveryRecords
from app.services.testing import TestService


class RecoveryError(Exception):
    """Sanitized terminal recovery failure; child records retain safe diagnostic codes."""


class RecoveryService:
    def __init__(
        self,
        settings: Settings,
        coding: CodingService,
        tests: TestService,
        debugger: DebugAgent,
        records: RecoveryRecords,
    ) -> None:
        self.settings, self.coding, self.tests = settings, coding, tests
        self.debugger, self.records = debugger, records

    async def run(
        self, run_id: UUID, task_id: UUID, context: IssueContext, config: RepositoryTestConfig
    ) -> RecoveryReport:
        limit = self.settings.max_recovery_attempts
        recovery_id, task, needs_coding = self.records.open(run_id, task_id, context, config, limit)
        report = RecoveryReport()
        try:
            if needs_coding:
                await self.coding.run(run_id, task_id, context)
            initial = self.tests.run(run_id, task_id, config, recovery_id=recovery_id)
            report.tests.append(initial)
            for _ in range(limit):
                if report.tests[-1].passed:
                    break
                report.debug_attempts += 1
                applied = await self._debug(recovery_id, run_id, context, task, report.tests[-1])
                if applied:
                    report.tests.append(
                        self.tests.run(run_id, task_id, config, recovery_id=recovery_id)
                    )
            report.passed = report.tests[-1].passed
            report.error = None if report.passed else "debug_attempts_exhausted"
            self.records.finish(recovery_id, report.passed, report.error)
            return report
        except asyncio.CancelledError:
            self.records.finish(recovery_id, False, "recovery_cancelled")
            raise
        except Exception:
            # Agent/test records preserve detailed sanitized failure evidence.
            self.records.finish(recovery_id, False, "recovery_operation_failed")
            raise RecoveryError("recovery_operation_failed") from None

    async def _debug(
        self,
        recovery_id: UUID,
        run_id: UUID,
        context: IssueContext,
        task: PlanTaskProposal,
        failed: TestReport,
    ) -> bool:
        agent_id, history = self.records.claim_debug(recovery_id, self.settings.llm_model)
        workspace = (
            self.settings.workspace_root / "executions" / str(context.repository.id) / str(run_id)
        )
        usage: TokenUsage | None = None
        applying = False
        try:
            with self.coding.patches.locked(workspace) as safe:
                inputs = DebugInput(
                    context=context,
                    task=task,
                    current_diff=self.coding.patches.current_diff(safe),
                    failing_tests=[
                        r.model_copy(update={"stdout": r.stdout[:4000], "stderr": r.stderr[:4000]})
                        for r in failed.results
                        if not r.passed
                    ],
                    previous_attempts=history,
                )
                serialized = inputs.model_dump_json()
                if len(serialized) > 200000 or self.coding.redact(serialized) != serialized:
                    raise PatchError("unsafe_or_oversized_debug_input")
                async with asyncio.timeout(self.settings.llm_timeout_seconds):
                    generated = await self.debugger.propose(inputs)
                usage = generated.usage
                proposal = generated.output
                if self.coding.redact(proposal.model_dump_json()) != proposal.model_dump_json():
                    raise PatchError("secret_in_debug_proposal")
                fingerprint = sha256(proposal.unified_diff.encode()).hexdigest()
                if not self.records.reserve_patch(recovery_id, agent_id, fingerprint):
                    raise PatchError("identical_debug_patch")
                applying = True
                self.coding.patches.apply(safe, proposal)
                diff = self.coding.patches.current_diff(safe)
                self.records.finish_debug(
                    recovery_id,
                    agent_id,
                    {
                        "summary": proposal.summary,
                        "proposal": proposal.model_dump(mode="json"),
                        "git_diff": self.coding.redact(diff),
                        "validation": "not_run",
                    },
                    True,
                    usage,
                )
            return True
        except asyncio.CancelledError:
            self.records.finish_debug(
                recovery_id, agent_id, {"error": "debug_cancelled"}, False, usage
            )
            raise
        except Exception as error:
            code = (
                error.code
                if isinstance(error, (PatchError, LLMError))
                else "debug_operation_failed"
            )
            if isinstance(error, TimeoutError):
                code = "llm_timeout"
            diagnostic = (
                self.coding.redact(error.diagnostic) if isinstance(error, PatchError) else ""
            )
            self.records.finish_debug(
                recovery_id,
                agent_id,
                {
                    "error": code,
                    "diagnostic": diagnostic,
                    "workspace_may_be_modified": True,
                },
                False,
                usage,
            )
            # If application started but post-apply inspection failed, fail closed: do not re-patch.
            if code in {"patch_diff_failed", "patch_diff_too_large", "debug_operation_failed"}:
                raise
            if applying and code in {
                "patch_apply_failed",
                "patch_git_timeout",
                "patch_git_unavailable",
            }:
                raise
            return False
