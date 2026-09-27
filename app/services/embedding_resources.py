"""Optional production provider lifecycle; tests inject providers directly."""

from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings
from app.integrations.llm.embeddings import EmbeddingError, EmbeddingProvider
from app.integrations.llm.openai_embeddings import OpenAIEmbeddingProvider
from app.models.repository import EMBEDDING_DIMENSION


@contextmanager
def embedding_resources(settings: Settings) -> Iterator[EmbeddingProvider | None]:
    if settings.llm_api_key is None or not settings.embedding_model:
        yield None
        return
    base = settings.llm_base_url or "https://api.openai.com/v1"
    parsed = urlsplit(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or settings.embedding_dim != EMBEDDING_DIMENSION
    ):
        raise EmbeddingError("embedding_invalid_configuration")
    with httpx.Client(
        base_url=base.rstrip("/") + "/",
        follow_redirects=False,
        headers={"Authorization": f"Bearer {settings.llm_api_key.get_secret_value()}"},
        timeout=settings.embedding_timeout_seconds,
    ) as client:
        yield OpenAIEmbeddingProvider(
            client,
            settings.embedding_model,
            settings.embedding_dim,
            batch_size=settings.embedding_batch_size,
            max_retries=settings.embedding_max_retries,
            send_dimensions=settings.embedding_send_dimensions,
        )
