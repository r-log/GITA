"""End-to-end pipeline tests: webhook HTTP -> dispatch -> runner.

These tests exercise the full path from an incoming GitHub webhook to the
runner function executing. The webhook layer uses a real FastAPI test
client with a fake ARQ pool (tracking enqueued jobs). The runner layer
is tested separately with a patched ``SessionLocal`` pointing at the
test DB.

External services (GitHub API, LLM) are mocked.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from gita.indexer.ingest import index_repository

SYNTH_REPO = (
    Path(__file__).parent.parent / "fixtures" / "synthetic_py"
).resolve()

TEST_SECRET = "e2e-pipeline-test-secret"
WEBHOOK_URL = "/api/webhooks/github"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
class _FakeJob:
    def __init__(self, job_id: str):
        self.job_id = job_id


class _FakeArqPool:
    """Tracks enqueued jobs by ID. Returns None on duplicate (like real ARQ)."""

    def __init__(self):
        self.jobs: dict[str, dict] = {}

    async def enqueue_job(self, function: str, *, _job_id: str | None = None, **kwargs):
        if _job_id and _job_id in self.jobs:
            return None
        key = _job_id or f"auto-{len(self.jobs)}"
        self.jobs[key] = {"function": function, **kwargs}
        return _FakeJob(key)


def _sign(body: bytes) -> str:
    digest = hmac.new(TEST_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _post(client, payload, *, event_type="push"):
    body = json.dumps(payload).encode()
    return client.post(
        WEBHOOK_URL,
        content=body,
        headers={
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Event": event_type,
            "X-GitHub-Delivery": "e2e-test-001",
            "Content-Type": "application/json",
        },
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def webhook_client(monkeypatch):
    """FastAPI TestClient with fake ARQ pool and known secret."""
    monkeypatch.setattr(
        "gita.web.webhooks.settings.github_webhook_secret", TEST_SECRET
    )
    from gita.web import cooldown, create_app

    cooldown.reset()
    app = create_app(use_lifespan=False)
    pool = _FakeArqPool()
    app.state.arq_pool = pool
    yield TestClient(app), pool
    cooldown.reset()


@pytest_asyncio.fixture
async def indexed_repo(db_session: AsyncSession):
    """Create an indexed repo with github_full_name in the test DB."""
    await index_repository(
        db_session,
        "synthetic_py",
        SYNTH_REPO,
        github_full_name="r-log/synthetic",
    )
    await db_session.flush()


@pytest_asyncio.fixture
async def _patch_runner_session(db_session: AsyncSession, monkeypatch):
    @asynccontextmanager
    async def _fake():
        yield db_session

    monkeypatch.setattr("gita.jobs.runners.SessionLocal", _fake)


# ---------------------------------------------------------------------------
# Push -> Reindex pipeline
# ---------------------------------------------------------------------------
class TestPushReindexPipeline:

    def test_push_webhook_enqueues_reindex(self, webhook_client):
        client, pool = webhook_client
        payload = {
            "repository": {"full_name": "r-log/synthetic"},
            "after": "abc123",
            "sender": {"login": "octocat", "type": "User"},
        }
        resp = _post(client, payload, event_type="push")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "dispatched"
        assert data["function"] == "reindex_repo"

        # Verify job was enqueued with correct params
        assert len(pool.jobs) == 1
        job = list(pool.jobs.values())[0]
        assert job["function"] == "reindex_repo"
        assert job["repo_full_name"] == "r-log/synthetic"
        assert job["after_sha"] == "abc123"

    @pytest.mark.usefixtures("indexed_repo", "_patch_runner_session")
    async def test_reindex_runner_updates_db(self, db_session: AsyncSession):
        """The runner (called with enqueued params) successfully re-indexes."""
        from gita.jobs.runners import run_reindex_job

        with patch("gita.jobs.runners._git_sync", return_value=(True, "")):
            result = await run_reindex_job(
                "r-log/synthetic", after_sha="abc123"
            )
        assert result["status"] == "completed"
        assert result["mode"] in ("full", "incremental", "noop")


# ---------------------------------------------------------------------------
# PR -> Review pipeline
# ---------------------------------------------------------------------------
class TestPrReviewPipeline:

    def test_pr_webhook_enqueues_review(self, webhook_client):
        client, pool = webhook_client
        payload = {
            "action": "opened",
            "pull_request": {"number": 42, "head": {"sha": "pr-sha-123"}},
            "repository": {"full_name": "r-log/synthetic"},
            "sender": {"login": "octocat", "type": "User"},
        }
        resp = _post(client, payload, event_type="pull_request")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "dispatched"
        assert data["function"] == "review_pr"

        job = list(pool.jobs.values())[0]
        assert job["repo_full_name"] == "r-log/synthetic"
        assert job["pr_number"] == 42
        assert job["head_sha"] == "pr-sha-123"

    @pytest.mark.usefixtures("indexed_repo", "_patch_runner_session")
    async def test_pr_review_resolves_indexed_repo(
        self, db_session: AsyncSession, monkeypatch
    ):
        """When repo is indexed, PR review passes resolution and fails later
        (at credential check, not repo lookup)."""
        from gita.jobs.runners import run_pr_review_job

        # Clear credentials so we hit the RuntimeError after resolution
        monkeypatch.setattr("gita.jobs.runners.settings.openrouter_api_key", "")

        # The RuntimeError at line ~120 proves resolution succeeded — it
        # didn't return {"reason": "repo_not_indexed"} at line ~102.
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            await run_pr_review_job("r-log/synthetic", 42)


# ---------------------------------------------------------------------------
# Issue -> Onboard pipeline
# ---------------------------------------------------------------------------
class TestIssueOnboardPipeline:

    def test_issue_webhook_enqueues_onboard(self, webhook_client):
        client, pool = webhook_client
        payload = {
            "action": "opened",
            "issue": {"number": 7},
            "repository": {"full_name": "r-log/synthetic"},
            "sender": {"login": "octocat", "type": "User"},
        }
        resp = _post(client, payload, event_type="issues")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "dispatched"
        assert data["function"] == "onboard_repo"

        job = list(pool.jobs.values())[0]
        assert job["repo_full_name"] == "r-log/synthetic"
        assert job["issue_number"] == 7


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------
class TestPipelineErrorPaths:

    @pytest.mark.usefixtures("_patch_runner_session")
    async def test_unindexed_repo_returns_error(self, db_session: AsyncSession):
        """Push for an unindexed repo -> runner returns error dict."""
        from gita.jobs.runners import run_reindex_job

        result = await run_reindex_job("r-log/nonexistent", after_sha="abc")
        assert result["status"] == "error"
        assert result["reason"] == "repo_not_indexed"

    def test_cooldown_blocks_duplicate_push(self, webhook_client):
        client, pool = webhook_client
        payload = {
            "repository": {"full_name": "r-log/synthetic"},
            "after": "abc123",
            "sender": {"login": "octocat", "type": "User"},
        }

        # First push — dispatched
        resp1 = _post(client, payload, event_type="push")
        assert resp1.json()["status"] == "dispatched"

        # Second push (same repo, within cooldown) — blocked
        resp2 = _post(client, payload, event_type="push")
        assert resp2.json()["status"] == "ignored"
        assert resp2.json()["reason"] == "cooldown"

    def test_bot_sender_blocked(self, webhook_client):
        client, pool = webhook_client
        payload = {
            "action": "opened",
            "pull_request": {"number": 1, "head": {"sha": "x"}},
            "repository": {"full_name": "r-log/synthetic"},
            "sender": {"login": "gita-agents[bot]", "type": "Bot"},
        }
        resp = _post(client, payload, event_type="pull_request")
        assert resp.json()["status"] == "ignored"
        assert resp.json()["reason"] == "bot sender"
        assert len(pool.jobs) == 0
