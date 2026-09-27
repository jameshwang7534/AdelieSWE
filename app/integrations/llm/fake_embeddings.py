"""Deterministic feature hashing for tests only; not a semantic model or production fallback."""

import hashlib
from collections.abc import Sequence

from app.integrations.llm.embeddings import EmbeddingError, profile_id, validate_vectors
from app.retrieval.tokenization import tokenize


class FakeEmbeddingProvider:
    def __init__(self, dimensions: int = 1536) -> None:
        if dimensions < 1:
            raise EmbeddingError("embedding_invalid_configuration")
        self.dimensions = dimensions
        self.profile = profile_id("test-only", "hashed-tokens-v1", dimensions)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in tokenize(text) or ["empty"]:
                index = (
                    int.from_bytes(hashlib.sha256(token.encode()).digest()[:8]) % self.dimensions
                )
                vector[index] += 1
            vectors.append(vector)
        return validate_vectors(vectors, len(texts), self.dimensions)
