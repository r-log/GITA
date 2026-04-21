"""Tests for health check endpoints.

No DB or Redis needed — the app is created with ``use_lifespan=False``
and the ARQ pool state is set directly on ``app.state``.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from gita.web import create_app


def _make_client(*, arq_pool=None) -> TestClient:
    app = create_app(use_lifespan=False)
    app.state.arq_pool = arq_pool
    return TestClient(app)


class TestLiveness:
    def test_returns_200(self):
        client = _make_client()
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_response_shape(self):
        client = _make_client()
        data = client.get("/health").json()
        assert data["status"] == "ok"

    def test_liveness_without_pool(self):
        """Liveness is always 200 — even when pool is None."""
        client = _make_client(arq_pool=None)
        assert client.get("/health").status_code == 200


class TestReadiness:
    def test_ready_with_pool(self):
        client = _make_client(arq_pool=object())  # truthy sentinel
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert data["arq_pool"] is True

    def test_not_ready_without_pool(self):
        client = _make_client(arq_pool=None)
        resp = client.get("/health/ready")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "not_ready"
        assert data["arq_pool"] is False

    def test_not_ready_when_pool_not_set(self):
        """If arq_pool was never set on state, readiness is 503."""
        app = create_app(use_lifespan=False)
        # Don't set app.state.arq_pool at all
        client = TestClient(app)
        assert client.get("/health/ready").status_code == 503
