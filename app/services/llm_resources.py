"""Explicit optional production client lifecycle; never initialized on API startup."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings
from app.integrations.llm.openai_provider import OpenAICompatibleLLMProvider
from app.integrations.llm.provider import LLMError, LLMProvider


@asynccontextmanager
async def llm_resources(settings: Settings) -> AsyncIterator[LLMProvider]:
    if (
        not settings.llm_api_key
        or not settings.llm_api_key.get_secret_value().strip()
        or not settings.llm_model
        or not settings.llm_model.strip()
    ):
        raise LLMError("llm_unconfigured")
    base = settings.llm_base_url or "https://api.openai.com/v1"
    try:
        parsed = urlsplit(base)
        _ = parsed.port  # Validate ports before HTTP client construction.
    except ValueError:
        raise LLMError("llm_invalid_configuration") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise LLMError("llm_invalid_configuration")
    async with httpx.AsyncClient(
        base_url=base.rstrip("/") + "/",
        follow_redirects=False,
        headers={"Authorization": f"Bearer {settings.llm_api_key.get_secret_value()}"},
        timeout=settings.llm_timeout_seconds,
    ) as client:
        yield OpenAICompatibleLLMProvider(
            client,
            settings.llm_model,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            max_output_tokens=settings.llm_max_output_tokens,
        )
