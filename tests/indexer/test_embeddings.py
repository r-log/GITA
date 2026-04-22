"""Tests for the embedding client module.

Uses the FakeEmbeddingClient — no OpenAI API calls needed.
"""
from __future__ import annotations

import math

from gita.indexer.embeddings import (
    EMBEDDING_DIMS,
    FakeEmbeddingClient,
    OpenAIEmbeddingClient,
    make_embedding_client,
)


class TestFakeEmbeddingClient:
    async def test_returns_correct_count(self):
        client = FakeEmbeddingClient()
        vectors = await client.embed(["hello", "world"])
        assert len(vectors) == 2

    async def test_correct_dimensions(self):
        client = FakeEmbeddingClient()
        vectors = await client.embed(["test text"])
        assert len(vectors[0]) == EMBEDDING_DIMS

    async def test_unit_vector(self):
        """Output vectors should be normalized to unit length."""
        client = FakeEmbeddingClient()
        vectors = await client.embed(["normalize me"])
        magnitude = math.sqrt(sum(x * x for x in vectors[0]))
        assert abs(magnitude - 1.0) < 1e-6

    async def test_deterministic(self):
        """Same text always produces the same embedding."""
        client = FakeEmbeddingClient()
        v1 = await client.embed(["hello"])
        v2 = await client.embed(["hello"])
        assert v1[0] == v2[0]

    async def test_different_texts_different_vectors(self):
        client = FakeEmbeddingClient()
        vectors = await client.embed(["hello", "goodbye"])
        assert vectors[0] != vectors[1]

    async def test_empty_input(self):
        client = FakeEmbeddingClient()
        vectors = await client.embed([])
        assert vectors == []

    async def test_tracks_calls(self):
        client = FakeEmbeddingClient()
        await client.embed(["a", "b", "c"])
        await client.embed(["d"])
        assert client.call_count == 2
        assert client.total_texts == 4

    async def test_custom_dimensions(self):
        client = FakeEmbeddingClient(dims=64)
        vectors = await client.embed(["test"])
        assert len(vectors[0]) == 64


class TestMakeEmbeddingClient:
    def test_returns_none_when_no_api_key(self, monkeypatch):
        from gita import config as config_module

        monkeypatch.setattr(config_module.settings, "openai_api_key", None)
        assert make_embedding_client() is None

    def test_returns_none_when_api_key_is_empty_string(self, monkeypatch):
        from gita import config as config_module

        monkeypatch.setattr(config_module.settings, "openai_api_key", "")
        assert make_embedding_client() is None

    def test_returns_openai_client_when_key_set(self, monkeypatch):
        from gita import config as config_module

        monkeypatch.setattr(
            config_module.settings, "openai_api_key", "sk-test"
        )
        client = make_embedding_client()
        assert isinstance(client, OpenAIEmbeddingClient)
