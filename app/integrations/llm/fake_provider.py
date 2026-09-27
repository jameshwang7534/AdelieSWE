"""Explicit deterministic JSON fixture provider; never a production fallback."""

from collections.abc import Sequence

from pydantic import BaseModel

from app.integrations.llm.provider import LLMError, LLMMessage, LLMResult, TokenUsage, parse_output


class FakeLLMProvider:
    def __init__(self, response_json: str, usage: TokenUsage | None = None) -> None:
        self.response_json, self.usage = response_json, usage or TokenUsage()

    async def generate[T: BaseModel](
        self, messages: Sequence[LLMMessage], response_model: type[T]
    ) -> LLMResult[T]:
        if not messages:
            raise LLMError("llm_invalid_configuration")
        return LLMResult(parse_output(self.response_json, response_model), self.usage.model_copy())
