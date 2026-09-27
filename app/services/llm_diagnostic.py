"""Small structured-output diagnostic, separate from the provider and future agents."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.integrations.llm.provider import LLMMessage, LLMProvider, LLMResult


class DiagnosticResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"]


async def run_diagnostic(provider: LLMProvider) -> LLMResult[DiagnosticResponse]:
    return await provider.generate(
        [LLMMessage(role="user", content='Return JSON with status set to "ok".')],
        DiagnosticResponse,
    )
