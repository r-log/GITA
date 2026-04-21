"""Embedding clients for semantic code search.

Provides a protocol-based abstraction so the ingest pipeline and views
can embed text without knowing which provider is behind it.

Two implementations:
- ``OpenAIEmbeddingClient``: real embeddings via OpenAI's API
  (``text-embedding-3-small``, 1536 dimensions).
- ``FakeEmbeddingClient``: deterministic vectors for testing (no API calls).
"""
from __future__ import annotations

import hashlib
import logging
import math
from typing import Protocol

logger = logging.getLogger(__name__)

# OpenAI text-embedding-3-small output dimensions.
EMBEDDING_DIMS = 1536

# Max texts per OpenAI embedding batch call.
_BATCH_SIZE = 2048


class EmbeddingClient(Protocol):
    """Async embedding client protocol."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text.

        Each vector has ``EMBEDDING_DIMS`` floats. The order of outputs
        matches the order of inputs.
        """
        ...


class OpenAIEmbeddingClient:
    """Real embeddings via OpenAI's ``text-embedding-3-small`` model.

    Uses the ``openai`` SDK with async support. Automatically batches
    requests when the input list exceeds ``_BATCH_SIZE``.
    """

    def __init__(self, api_key: str, *, model: str = "text-embedding-3-small"):
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        all_vectors: list[list[float]] = []
        for start in range(0, len(texts), _BATCH_SIZE):
            batch = texts[start : start + _BATCH_SIZE]
            response = await self._client.embeddings.create(
                input=batch,
                model=self._model,
            )
            # Response data is sorted by index — extract in order.
            sorted_data = sorted(response.data, key=lambda d: d.index)
            all_vectors.extend(d.embedding for d in sorted_data)

        return all_vectors

    async def close(self) -> None:
        await self._client.close()


class FakeEmbeddingClient:
    """Deterministic embeddings for testing. No API calls.

    Produces a unit vector derived from the SHA-256 hash of each input
    text. Vectors are reproducible: the same text always yields the same
    embedding. Dimensionality matches ``EMBEDDING_DIMS``.
    """

    def __init__(self, *, dims: int = EMBEDDING_DIMS):
        self._dims = dims
        self.call_count = 0
        self.total_texts = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.call_count += 1
        self.total_texts += len(texts)
        return [self._deterministic_vector(t) for t in texts]

    def _deterministic_vector(self, text: str) -> list[float]:
        """Hash-based pseudo-random unit vector."""
        digest = hashlib.sha256(text.encode()).digest()
        # Expand the 32-byte digest to fill EMBEDDING_DIMS floats.
        raw: list[float] = []
        for i in range(self._dims):
            byte_val = digest[i % len(digest)]
            raw.append((byte_val / 255.0) * 2 - 1)  # range [-1, 1]
        # Normalize to unit vector.
        magnitude = math.sqrt(sum(x * x for x in raw))
        if magnitude > 0:
            raw = [x / magnitude for x in raw]
        return raw

    async def close(self) -> None:
        pass
