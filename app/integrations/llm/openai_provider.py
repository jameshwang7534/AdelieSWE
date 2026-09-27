"""Async Chat Completions adapter; callers own prompts and HTTP client lifecycle."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence

import httpx
from pydantic import BaseModel, ValidationError

from app.integrations.llm.provider import LLMError, LLMMessage, LLMResult, TokenUsage, parse_output
from app.integrations.llm.structured_schema import structured_schema


class OpenAICompatibleLLMProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        model: str,
        *,
        timeout: float = 30,
        max_retries: int = 3,
        max_output_tokens: int = 2048,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not model.strip() or timeout <= 0 or not 0 <= max_retries <= 5 or max_output_tokens < 1:
            raise LLMError("llm_invalid_configuration")
        self.client, self.model, self.timeout = client, model, timeout
        self.max_retries, self.max_output_tokens, self.sleep = max_retries, max_output_tokens, sleep

    async def generate[T: BaseModel](
        self, messages: Sequence[LLMMessage], response_model: type[T]
    ) -> LLMResult[T]:
        if not messages:
            raise LLMError("llm_invalid_configuration")
        payload = {
            "model": self.model,
            "messages": [message.model_dump() for message in messages],
            "max_completion_tokens": self.max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_response",
                    "strict": True,
                    "schema": structured_schema(response_model),
                },
            },
        }
        for attempt in range(self.max_retries + 1):
            delay = float(min(2**attempt, 8))
            try:
                async with asyncio.timeout(self.timeout):
                    response = await self.client.post(
                        "chat/completions",
                        json=payload,
                        timeout=self.timeout,
                        follow_redirects=False,
                    )
            except (httpx.TimeoutException, TimeoutError):
                error = LLMError("llm_timeout")
            except httpx.RequestError:
                error = LLMError("llm_unavailable")
            else:
                if response.is_success:
                    return self._decode(response, response_model)
                if response.status_code in (401, 403):
                    raise LLMError("llm_authentication_failed")
                if response.status_code not in (408, 429) and response.status_code < 500:
                    raise LLMError("llm_request_rejected")
                error = LLMError(
                    "llm_rate_limited" if response.status_code == 429 else "llm_unavailable"
                )
                retry_after = response.headers.get("retry-after", "")
                if retry_after.isascii() and retry_after.isdigit():
                    delay = max(delay, float(retry_after))
            if attempt == self.max_retries or delay > 60:
                raise error from None
            await self.sleep(delay)
        raise LLMError("llm_unavailable")

    def _decode[T: BaseModel](self, response: httpx.Response, model: type[T]) -> LLMResult[T]:
        try:
            payload = response.json()
            choice = payload["choices"][0]
            message = choice["message"]
            if message.get("refusal"):
                raise LLMError("llm_refused")
            if choice["finish_reason"] != "stop":
                raise LLMError("llm_incomplete")
            content = message["content"]
            if not isinstance(content, str):
                raise ValueError
            usage = payload.get("usage") or {}
            tokens = TokenUsage(
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
            )
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, ValidationError):
            raise LLMError("llm_invalid_response") from None
        return LLMResult(parse_output(content, model), tokens)
