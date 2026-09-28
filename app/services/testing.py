"""Explicit trusted test execution; no automatic debug, retry, or worker dispatch."""

from uuid import UUID

from app.agents.testing import TestAgent, TestPolicyError
from app.core.config import Settings
from app.sandbox.base import Sandbox, SandboxRequest
from app.schemas.testing import RepositoryTestConfig, TestReport, TestResult
from app.services.code_patches import CodePatchService
from app.services.test_records import TestRecords


class TestExecutionError(Exception):
    """Fixed, safe failure code; prior results remain persisted."""


class TestService:
    def __init__(self, settings: Settings, sandbox: Sandbox, records: TestRecords) -> None:
        self.settings, self.sandbox, self.records = settings, sandbox, records

    def _redact(self, value: str) -> str:
        for secret in (
            self.settings.github_token,
            self.settings.llm_api_key,
            self.settings.database_url,
            self.settings.redis_url,
        ):
            if secret and secret.get_secret_value():
                value = value.replace(secret.get_secret_value(), "[REDACTED]")
        return value[: self.settings.sandbox_output_bytes]

    def run(self, run_id: UUID, task_id: UUID, config: RepositoryTestConfig) -> TestReport:
        agent_id, inputs = self.records.start(run_id, task_id, config)
        report = TestReport(results=[], passed=False)
        workspace = (
            self.settings.workspace_root / "executions" / str(inputs.repository.id) / str(run_id)
        )
        try:
            commands = TestAgent(self.settings.test_allowed_commands).select(inputs)
            if any(item.image not in self.settings.sandbox_images for item in commands):
                raise TestPolicyError("test_image_not_configured")
            with CodePatchService(self.settings.workspace_root).locked(workspace):
                for item in commands:
                    result = self.sandbox.execute(
                        SandboxRequest(
                            workspace=workspace,
                            image=item.image,
                            command=item.command,
                            timeout=self.settings.sandbox_timeout_seconds,
                        )
                    )
                    report.results.append(
                        TestResult(
                            command=item.command,
                            exit_code=result.exit_code,
                            stdout=self._redact(result.stdout),
                            stderr=self._redact(result.stderr),
                            duration=result.elapsed_seconds,
                            timeout=result.timed_out,
                            passed=result.exit_code == 0 and not result.timed_out,
                            output_truncated=result.output_truncated,
                        )
                    )
                report.passed = bool(report.results) and all(r.passed for r in report.results)
                self.records.finish(run_id, task_id, agent_id, report)
            return report
        except Exception as error:
            code = str(error) if isinstance(error, TestPolicyError) else "test_execution_failed"
            report.passed = False
            self.records.finish(run_id, task_id, agent_id, report, code)
            raise TestExecutionError(code) from None
