"""Provider-neutral async structured generation contract and sanitized errors."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, Field, ValidationError

ErrorCode = Literal[
    "llm_unconfigured",
    "llm_invalid_configuration",
    "llm_authentication_failed",
    "llm_rate_limited",
    "llm_timeout",
    "llm_unavailable",
    "llm_request_rejected",
    "llm_invalid_response",
    "llm_refused",
    "llm_incomplete",
]


class LLMError(Exception):
    def __init__(self, code: ErrorCode) -> None:
        self.code = code
        super().__init__(code)


class LLMMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class TokenUsage(BaseModel):
    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    total_tokens: int | None = Field(default=None, ge=0, strict=True)


@dataclass(frozen=True)
class LLMResult[T: BaseModel]:
    output: T
    usage: TokenUsage


class LLMProvider(Protocol):
    async def generate[T: BaseModel](
        self, messages: Sequence[LLMMessage], response_model: type[T]
    ) -> LLMResult[T]: ...


def parse_output[T: BaseModel](content: str, response_model: type[T]) -> T:
    try:
        return response_model.model_validate_json(content, strict=True)
    except ValidationError:
        raise LLMError("llm_invalid_response") from None
