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


def validation_feedback(error: ValidationError) -> tuple[str, ...]:
    """Bounded diagnostic categories; never include rejected values or raw exception text."""
    dag_errors = {
        "Task keys must be unique",
        "Task cannot depend on itself",
        "Duplicate task dependencies",
        "Dependency references an unknown task",
        "Plan dependencies contain a cycle",
        "Plan must contain at least one task",
    }
    feedback = []
    for item in error.errors(include_input=False, include_context=False, include_url=False)[:8]:
        message = item["msg"].removeprefix("Value error, ")
        feedback.append(message if message in dag_errors else item["type"])
    return tuple(feedback)


class LLMValidationError(LLMError):
    def __init__(self, feedback: tuple[str, ...]) -> None:
        super().__init__("llm_invalid_response")
        self.feedback = feedback


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
    except ValidationError as error:
        raise LLMValidationError(validation_feedback(error)) from None
