"""OpenAI-compatible /embeddings adapter using a caller-owned HTTP client."""

import time
from collections.abc import Callable, Sequence

import httpx

from app.integrations.llm.embeddings import EmbeddingError, profile_id, validate_vectors


class OpenAIEmbeddingProvider:
    def __init__(
        self,
        client: httpx.Client,
        model: str,
        dimensions: int,
        *,
        batch_size: int = 16,
        max_retries: int = 3,
        send_dimensions: bool = True,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not model or dimensions < 1 or batch_size < 1 or max_retries < 0:
            raise EmbeddingError("embedding_invalid_configuration")
        self.client, self.model, self.dimensions = client, model, dimensions
        self.profile = profile_id(str(client.base_url), model, dimensions)
        self.batch_size, self.max_retries = batch_size, max_retries
        self.sleep, self.send_dimensions = sleep, send_dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if any(not text.strip() for text in texts):
            raise EmbeddingError("embedding_empty_input")
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._batch(list(texts[start : start + self.batch_size])))
        return vectors

    def _batch(self, texts: list[str]) -> list[list[float]]:
        payload: dict[str, object] = {
            "model": self.model,
            "input": texts,
            "encoding_format": "float",
        }
        if self.send_dimensions:
            payload["dimensions"] = self.dimensions
        for attempt in range(self.max_retries + 1):
            delay = float(min(2**attempt, 8))
            try:
                response = self.client.post("embeddings", json=payload)
            except httpx.RequestError:
                if attempt == self.max_retries:
                    raise EmbeddingError("embedding_unavailable") from None
            else:
                if response.is_success:
                    return self._decode(response, len(texts))
                if response.status_code not in {408, 429} and response.status_code < 500:
                    raise EmbeddingError("embedding_request_rejected")
                retry = response.headers.get("retry-after", "")
                if retry.isdigit():
                    delay = max(delay, float(retry))
                if attempt == self.max_retries or delay > 60:
                    raise EmbeddingError("embedding_unavailable")
            self.sleep(delay)
        raise EmbeddingError("embedding_unavailable")

    def _decode(self, response: httpx.Response, count: int) -> list[list[float]]:
        try:
            payload = response.json()
            data = payload["data"]
            if not isinstance(data, list) or len(data) != count:
                raise ValueError
            by_index = {}
            for item in data:
                index = item["index"]
                if type(index) is not int or index in by_index or not 0 <= index < count:
                    raise ValueError
                by_index[index] = item["embedding"]
            ordered = [by_index[index] for index in range(count)]
        except (ValueError, TypeError, KeyError):
            raise EmbeddingError("embedding_invalid_response") from None
        return validate_vectors(ordered, count, self.dimensions)
