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
SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40
REPO = {"full_name": "r-log/synthetic", "default_branch": "main"}
USER = {"login": "octocat", "type": "User"}
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


def _post(client, payload, *, event_type="push", delivery="e2e-test-001"):
    body = json.dumps(payload).encode()
    return client.post(
        WEBHOOK_URL,
        content=body,
        headers={
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Event": event_type,
            "X-GitHub-Delivery": delivery,
            "Content-Type": "application/json",
        },
    )


def _push(ref: str, after: str, *, deleted: bool = False) -> dict:
    return {"ref": ref, "after": after, "deleted": deleted, "repository": REPO, "sender": USER}


def _pr(action: str, number: int, head_sha: str) -> dict:
    return {
        "action": action,
        "pull_request": {"number": number, "head": {"sha": head_sha}, "draft": False},
        "repository": REPO,
        "sender": USER,
    }


def _issue_labeled(number: int, *, sender: str, author: str, association: str) -> dict:
    return {
        "action": "labeled",
        "label": {"name": "gita:onboard"},
        "issue": {
            "number": number,
            "user": {"login": author},
            "author_association": association,
        },
        "repository": REPO,
        "sender": {"login": sender, "type": "User"},
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def webhook_client(monkeypatch):
    """FastAPI TestClient with fake ARQ pool and known secret."""
    monkeypatch.setattr(
        "gita.web.webhooks.settings.github_webhook_secret", TEST_SECRET
    )
    from gita.web import create_app

    app = create_app(use_lifespan=False)
    pool = _FakeArqPool()
    app.state.arq_pool = pool
    yield TestClient(app), pool


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
# A realistic delivery log, end to end through the HTTP layer
# ---------------------------------------------------------------------------
class TestRealisticDeliverySequence:
    """What GitHub actually sends during an ordinary afternoon.

    Each step is (event, payload, expected outcome). The previous design
    dropped the review in step 2 (per-repo cooldown after the push in
    step 1), the review in step 4 (job ID without SHA still held by the
    first review's result), reindexed feature branches, and ran
    onboarding for every opened issue.
    """

    def test_exactly_the_right_jobs_are_enqueued(self, webhook_client):
        client, pool = webhook_client
        steps = [
            # Developer pushes a branch and opens a PR straight away.
            ("push", _push("refs/heads/feature", SHA_A), "ignored"),
            ("pull_request", _pr("opened", 7, SHA_A), "dispatched"),
            # Pushes a fix: GitHub sends push + synchronize together.
            ("push", _push("refs/heads/feature", SHA_B), "ignored"),
            ("pull_request", _pr("synchronize", 7, SHA_B), "dispatched"),
            # GitHub redelivers the synchronize.
            ("pull_request", _pr("synchronize", 7, SHA_B), "ignored"),
            # PR is merged: push to the default branch, then branch deleted.
            ("push", _push("refs/heads/main", SHA_C), "dispatched"),
            ("push", _push("refs/heads/feature", "0" * 40, deleted=True), "ignored"),
            ("push", _push("refs/tags/v1.0", SHA_C), "ignored"),
            # Someone opens an issue whose template applies the label.
            (
                "issues",
                _issue_labeled(3, sender="drive-by", author="drive-by", association="NONE"),
                "ignored",
            ),
            # A maintainer asks for onboarding on that issue.
            (
                "issues",
                _issue_labeled(3, sender="maintainer", author="drive-by", association="NONE"),
                "dispatched",
            ),
        ]
        for i, (event, payload, expected) in enumerate(steps):
            resp = _post(client, payload, event_type=event, delivery=f"d-{i}")
            assert resp.status_code == 200, (i, resp.text)
            assert resp.json()["status"] == expected, (i, event, resp.json())

        assert list(pool.jobs) == [
            f"review-pr:r-log/synthetic:7:{SHA_A}",
            f"review-pr:r-log/synthetic:7:{SHA_B}",
            f"reindex:r-log/synthetic:{SHA_C}",
            "onboard:r-log/synthetic:3",
        ]
        # Reviews are deferred so the stale one can see it's superseded.
        review = pool.jobs[f"review-pr:r-log/synthetic:7:{SHA_B}"]
        assert review["_defer_by"] > 0
        assert review["head_sha"] == SHA_B


# ---------------------------------------------------------------------------
# Push -> Reindex pipeline
# ---------------------------------------------------------------------------
class TestPushReindexPipeline:
    def test_push_webhook_enqueues_reindex(self, webhook_client):
        client, pool = webhook_client
        resp = _post(client, _push("refs/heads/main", SHA_A), event_type="push")
        assert resp.status_code == 200
        assert resp.json()["function"] == "reindex_repo"
        job = pool.jobs[f"reindex:r-log/synthetic:{SHA_A}"]
        assert job["repo_full_name"] == "r-log/synthetic"
        assert job["after_sha"] == SHA_A

    @pytest.mark.usefixtures("indexed_repo", "_patch_runner_session")
    async def test_reindex_runner_updates_db(self, db_session: AsyncSession):
        """The runner (called with enqueued params) successfully re-indexes."""
        from gita.jobs.runners import run_reindex_job

        with patch("gita.jobs.runners._git_sync", return_value=(True, "")):
            result = await run_reindex_job("r-log/synthetic", after_sha=SHA_A)
        assert result["status"] == "completed"
        assert result["mode"] in ("full", "incremental", "noop")


# ---------------------------------------------------------------------------
# PR -> Review pipeline
# ---------------------------------------------------------------------------
class TestPrReviewPipeline:
    def test_pr_webhook_enqueues_review(self, webhook_client):
        client, pool = webhook_client
        resp = _post(client, _pr("opened", 42, SHA_A), event_type="pull_request")
        assert resp.json()["status"] == "dispatched"
        job = pool.jobs[f"review-pr:r-log/synthetic:42:{SHA_A}"]
        assert job["repo_full_name"] == "r-log/synthetic"
        assert job["pr_number"] == 42
        assert job["head_sha"] == SHA_A

    @pytest.mark.usefixtures("indexed_repo", "_patch_runner_session")
    async def test_pr_review_resolves_indexed_repo(
        self, db_session: AsyncSession, monkeypatch
    ):
        """Resolution succeeds, so the job fails later at the credential check."""
        from gita.jobs.runners import run_pr_review_job

        monkeypatch.setattr("gita.jobs.runners.settings.openrouter_api_key", "")
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            await run_pr_review_job("r-log/synthetic", 42)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------
class TestPipelineErrorPaths:
    @pytest.mark.usefixtures("_patch_runner_session")
    async def test_unindexed_repo_returns_error(self, db_session: AsyncSession):
        """Push for an unindexed repo -> runner returns error dict."""
        from gita.jobs.runners import run_reindex_job

        result = await run_reindex_job("r-log/nonexistent", after_sha=SHA_A)
        assert result["status"] == "error"
        assert result["reason"] == "repo_not_indexed"

    def test_bot_sender_blocked(self, webhook_client):
        client, pool = webhook_client
        payload = _pr("opened", 1, SHA_A)
        payload["sender"] = {"login": "gita-agents[bot]", "type": "Bot"}
        resp = _post(client, payload, event_type="pull_request")
        assert resp.json()["status"] == "ignored"
        assert resp.json()["reason"] == "bot sender"
        assert len(pool.jobs) == 0
