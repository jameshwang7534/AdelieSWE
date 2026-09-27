"""Explicit deterministic JSON fixture provider; never a production fallback."""

from collections.abc import Sequence

from pydantic import BaseModel

from app.integrations.llm.provider import LLMError, LLMMessage, LLMResult, TokenUsage, parse_output


class FakeLLMProvider:
    def __init__(self, response_json: str | Sequence[str], usage: TokenUsage | None = None) -> None:
        self.responses = (
            (response_json,) if isinstance(response_json, str) else tuple(response_json)
        )
        if not self.responses:
            raise ValueError("At least one fixture response is required")
        self.usage = usage or TokenUsage()
        self.calls: list[tuple[LLMMessage, ...]] = []

    async def generate[T: BaseModel](
        self, messages: Sequence[LLMMessage], response_model: type[T]
    ) -> LLMResult[T]:
        if not messages:
            raise LLMError("llm_invalid_configuration")
        # Repeat the final fixture so exhaustion/repair tests remain deterministic.
        response = self.responses[min(len(self.calls), len(self.responses) - 1)]
        self.calls.append(tuple(message.model_copy(deep=True) for message in messages))
        return LLMResult(parse_output(response, response_model), self.usage.model_copy())
