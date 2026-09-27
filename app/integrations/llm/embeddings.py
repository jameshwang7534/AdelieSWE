"""Embedding contract and strict vector validation shared by production and test providers."""

import hashlib
import math
from collections.abc import Sequence
from typing import Protocol


class EmbeddingError(Exception):
    """Sanitized provider/configuration failure; never contains input or credentials."""


class EmbeddingUnconfigured(EmbeddingError):
    """Production generation was requested without the required settings."""


class EmbeddingProvider(Protocol):
    @property
    def dimensions(self) -> int: ...
    @property
    def profile(self) -> str: ...
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def profile_id(base_url: str, model: str, dimensions: int) -> str:
    return hashlib.sha256(f"{base_url.rstrip('/')}\n{model}\n{dimensions}".encode()).hexdigest()


def validate_vectors(vectors: object, count: int, dimensions: int) -> list[list[float]]:
    if not isinstance(vectors, list) or len(vectors) != count:
        raise EmbeddingError("embedding_invalid_response")
    result = []
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != dimensions:
            raise EmbeddingError("embedding_dimension_mismatch")
        if any(type(value) not in (int, float) for value in vector):
            raise EmbeddingError("embedding_invalid_vector")
        try:
            values = [float(value) for value in vector]
            norm = math.hypot(*values)
        except (OverflowError, ValueError):
            raise EmbeddingError("embedding_invalid_vector") from None
        if not math.isfinite(norm) or norm == 0:
            raise EmbeddingError("embedding_invalid_vector")
        result.append([value / norm for value in values])
    return result
