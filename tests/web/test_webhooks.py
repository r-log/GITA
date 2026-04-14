"""Tests for the GitHub webhook receiver endpoint.

These tests exercise the FastAPI HTTP layer — HMAC verification, bot
sender filtering, event parsing, and edge cases. No DB or Redis needed.

The test secret and payloads are synthetic. HMAC signatures are computed
inline so the tests are self-contained.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from gita.web.webhooks import WebhookEvent, _parse_event, verify_signature

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TEST_SECRET = "test-webhook-secret-for-unit-tests"
WEBHOOK_URL = "/api/webhooks/github"


class _FakeJob:
    """Minimal fake returned by FakeArqPool.enqueue_job."""

    def __init__(self, job_id: str):
        self.job_id = job_id


class _FakeArqPool:
    """In-memory fake for ``ArqRedis`` — tracks enqueued jobs by ID.

    Returns ``None`` on duplicate ``_job_id`` (matches real ARQ behavior
    for Wall 3 testing).
    """

    def __init__(self):
        self._jobs: dict[str, dict] = {}

    async def enqueue_job(self, function: str, *, _job_id: str | None = None, **kwargs):
        if _job_id and _job_id in self._jobs:
            return None
        key = _job_id or f"auto-{len(self._jobs)}"
        self._jobs[key] = {"function": function, **kwargs}
        return _FakeJob(key)


def _sign(body: bytes, secret: str = TEST_SECRET) -> str:
    """Compute a valid X-Hub-Signature-256 header value."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _make_payload(
    *,
    action: str | None = "opened",
    sender_type: str = "User",
    sender_login: str = "octocat",
    repo_full_name: str = "r-log/AMASS",
    pr_number: int | None = 42,
    issue_number: int | None = None,
    extra: dict | None = None,
) -> dict:
    """Build a GitHub webhook payload.

    Includes ``pull_request.number`` by default so dispatch can route
    PR events. Pass ``pr_number=None`` to omit it.
    """
    payload: dict = {
        "sender": {"login": sender_login, "type": sender_type},
        "repository": {"full_name": repo_full_name},
    }
    if action is not None:
        payload["action"] = action
    if pr_number is not None:
        payload["pull_request"] = {
            "number": pr_number,
            "head": {"sha": "test-sha-abc123"},
        }
    if issue_number is not None:
        payload["issue"] = {"number": issue_number}
    if extra:
        payload.update(extra)
    return payload


def _post_webhook(
    client: TestClient,
    payload: dict,
    *,
    event_type: str = "pull_request",
    secret: str = TEST_SECRET,
    signature: str | None = None,
    include_signature: bool = True,
    include_event_header: bool = True,
    delivery_id: str = "test-delivery-001",
) -> TestClient:
    """Helper to POST a webhook with the right headers."""
    body = json.dumps(payload).encode()
    headers: dict[str, str] = {}

    if include_signature:
        headers["X-Hub-Signature-256"] = signature or _sign(body, secret)
    if include_event_header:
        headers["X-GitHub-Event"] = event_type
    headers["X-GitHub-Delivery"] = delivery_id
    headers["Content-Type"] = "application/json"

    return client.post(WEBHOOK_URL, content=body, headers=headers)


# ---------------------------------------------------------------------------
# Fixture: patched app with known secret
# ---------------------------------------------------------------------------
@pytest.fixture
def client(monkeypatch):
    """TestClient with GITHUB_WEBHOOK_SECRET set and a fake ARQ pool."""
    monkeypatch.setattr(
        "gita.web.webhooks.settings.github_webhook_secret", TEST_SECRET
    )
    from gita.web import cooldown
    cooldown.reset()

    from gita.web import create_app

    app = create_app(use_lifespan=False)
    app.state.arq_pool = _FakeArqPool()
    yield TestClient(app)
    cooldown.reset()


@pytest.fixture
def client_no_secret(monkeypatch):
    """TestClient with no webhook secret configured."""
    monkeypatch.setattr(
        "gita.web.webhooks.settings.github_webhook_secret", None
    )
    from gita.web import create_app

    app = create_app(use_lifespan=False)
    app.state.arq_pool = _FakeArqPool()
    return TestClient(app)


# ---------------------------------------------------------------------------
# Pure function: verify_signature
# ---------------------------------------------------------------------------
class TestVerifySignature:
    def test_valid_signature(self):
        body = b'{"hello": "world"}'
        sig = _sign(body)
        assert verify_signature(body, sig, TEST_SECRET) is True

    def test_invalid_signature(self):
        body = b'{"hello": "world"}'
        assert verify_signature(body, "sha256=deadbeef", TEST_SECRET) is False

    def test_wrong_prefix(self):
        body = b'{"hello": "world"}'
        digest = hmac.new(
            TEST_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
        assert verify_signature(body, f"sha1={digest}", TEST_SECRET) is False

    def test_empty_body(self):
        body = b""
        sig = _sign(body)
        assert verify_signature(body, sig, TEST_SECRET) is True

    def test_different_secret_fails(self):
        body = b'{"test": true}'
        sig = _sign(body, "correct-secret")
        assert verify_signature(body, sig, "wrong-secret") is False


# ---------------------------------------------------------------------------
# Pure function: _parse_event
# ---------------------------------------------------------------------------
class TestParseEvent:
    def test_full_payload(self):
        payload = _make_payload(
            action="opened",
            sender_login="octocat",
            sender_type="User",
            repo_full_name="r-log/AMASS",
        )
        event = _parse_event(payload, "pull_request", "delivery-123")

        assert event.event_type == "pull_request"
        assert event.action == "opened"
        assert event.delivery_id == "delivery-123"
        assert event.repo_full_name == "r-log/AMASS"
        assert event.sender_login == "octocat"
        assert event.sender_type == "User"

    def test_push_event_no_action(self):
        payload = _make_payload(action=None)
        event = _parse_event(payload, "push", None)

        assert event.event_type == "push"
        assert event.action is None

    def test_missing_sender(self):
        payload = {"repository": {"full_name": "r-log/AMASS"}}
        event = _parse_event(payload, "issues", None)

        assert event.sender_login is None
        assert event.sender_type is None

    def test_missing_repository(self):
        payload = {"sender": {"login": "bot", "type": "Bot"}}
        event = _parse_event(payload, "issues", None)

        assert event.repo_full_name is None


# ---------------------------------------------------------------------------
# HTTP: signature verification
# ---------------------------------------------------------------------------
class TestSignatureVerification:
    def test_valid_signature_returns_200(self, client):
        payload = _make_payload()
        resp = _post_webhook(client, payload)
        assert resp.status_code == 200

    def test_invalid_signature_returns_401(self, client):
        payload = _make_payload()
        resp = _post_webhook(client, payload, signature="sha256=badhash")
        assert resp.status_code == 401
        assert "invalid signature" in resp.json()["error"]

    def test_missing_signature_returns_401(self, client):
        payload = _make_payload()
        resp = _post_webhook(client, payload, include_signature=False)
        assert resp.status_code == 401
        assert "missing" in resp.json()["error"].lower()

    def test_no_secret_configured_returns_500(self, client_no_secret):
        payload = _make_payload()
        body = json.dumps(payload).encode()
        headers = {
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Event": "pull_request",
            "Content-Type": "application/json",
        }
        resp = client_no_secret.post(WEBHOOK_URL, content=body, headers=headers)
        assert resp.status_code == 500
        assert "not configured" in resp.json()["error"]


# ---------------------------------------------------------------------------
# HTTP: bot sender filter (Wall 2)
# ---------------------------------------------------------------------------
class TestBotSenderFilter:
    def test_bot_sender_ignored(self, client):
        payload = _make_payload(
            sender_type="Bot", sender_login="gita-agents[bot]"
        )
        resp = _post_webhook(client, payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ignored"
        assert data["reason"] == "bot sender"

    def test_user_sender_dispatched(self, client):
        payload = _make_payload(sender_type="User", sender_login="octocat")
        resp = _post_webhook(client, payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "dispatched"

    def test_organization_sender_dispatched(self, client):
        """GitHub Org-owned actions fire with sender.type=Organization."""
        payload = _make_payload(
            sender_type="Organization", sender_login="r-log"
        )
        resp = _post_webhook(client, payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "dispatched"


# ---------------------------------------------------------------------------
# HTTP: event type parsing
# ---------------------------------------------------------------------------
class TestEventTypeParsing:
    def test_pull_request_opened_dispatched(self, client):
        payload = _make_payload(action="opened")
        resp = _post_webhook(client, payload, event_type="pull_request")
        data = resp.json()
        assert data["status"] == "dispatched"
        assert data["function"] == "review_pr"

    def test_pull_request_synchronize_dispatched(self, client):
        payload = _make_payload(action="synchronize")
        resp = _post_webhook(client, payload, event_type="pull_request")
        data = resp.json()
        assert data["status"] == "dispatched"
        assert data["function"] == "review_pr"

    def test_issues_opened_dispatched(self, client):
        payload = _make_payload(action="opened", pr_number=None, issue_number=7)
        resp = _post_webhook(client, payload, event_type="issues")
        data = resp.json()
        assert data["status"] == "dispatched"
        assert data["function"] == "onboard_repo"

    def test_push_event_dispatched(self, client):
        payload = _make_payload(action=None, pr_number=None, extra={"after": "abc123"})
        resp = _post_webhook(client, payload, event_type="push")
        data = resp.json()
        assert data["status"] == "dispatched"
        assert data["function"] == "reindex_repo"

    def test_unhandled_event_ignored(self, client):
        """Events not in EVENT_HANDLERS return 200 with 'no handler'."""
        payload = _make_payload(action="created", pr_number=None)
        resp = _post_webhook(client, payload, event_type="star")
        data = resp.json()
        assert data["status"] == "ignored"
        assert data["reason"] == "no handler"

    def test_missing_event_header_returns_400(self, client):
        payload = _make_payload()
        resp = _post_webhook(
            client, payload, include_event_header=False
        )
        assert resp.status_code == 400
        assert "missing" in resp.json()["error"].lower()


# ---------------------------------------------------------------------------
# HTTP: ping event
# ---------------------------------------------------------------------------
class TestPingEvent:
    def test_ping_returns_pong(self, client):
        payload = {
            "zen": "Speak like a human.",
            "hook_id": 12345,
            "hook": {"type": "App", "id": 12345},
        }
        resp = _post_webhook(client, payload, event_type="ping")
        assert resp.status_code == 200
        assert resp.json()["status"] == "pong"


# ---------------------------------------------------------------------------
# HTTP: delivery id forwarding
# ---------------------------------------------------------------------------
class TestDeliveryId:
    def test_delivery_id_logged(self, client):
        """Delivery ID should be accepted and parsed (used for tracing)."""
        payload = _make_payload()
        resp = _post_webhook(
            client, payload, delivery_id="550e8400-e29b-41d4-a716-446655440000"
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "dispatched"


# ---------------------------------------------------------------------------
# HTTP: ARQ pool integration
# ---------------------------------------------------------------------------
class TestArqPoolIntegration:
    def test_no_pool_returns_503(self, monkeypatch):
        """If the ARQ pool is unavailable, return 503."""
        monkeypatch.setattr(
            "gita.web.webhooks.settings.github_webhook_secret", TEST_SECRET
        )
        from gita.web import create_app, cooldown
        cooldown.reset()

        app = create_app(use_lifespan=False)
        app.state.arq_pool = None  # simulate pool failure
        no_pool_client = TestClient(app)

        payload = _make_payload()
        resp = _post_webhook(no_pool_client, payload)
        assert resp.status_code == 503
        assert "unavailable" in resp.json()["error"]
        cooldown.reset()

    def test_duplicate_job_id_ignored(self, monkeypatch):
        """Wall 3: second webhook for same PR returns 'job already queued'.

        We set cooldown window to 0 so the cooldown gate doesn't fire
        first — this isolates the ARQ job ID deduplication behavior.
        """
        monkeypatch.setattr(
            "gita.web.webhooks.settings.github_webhook_secret", TEST_SECRET
        )
        monkeypatch.setattr(
            "gita.web.webhooks.check_cooldown",
            lambda repo, **kw: False,  # bypass cooldown
        )
        from gita.web import create_app, cooldown
        cooldown.reset()

        app = create_app(use_lifespan=False)
        app.state.arq_pool = _FakeArqPool()
        dup_client = TestClient(app)

        payload = _make_payload(pr_number=42)

        resp1 = _post_webhook(dup_client, payload)
        assert resp1.json()["status"] == "dispatched"

        # Same PR again — the FakeArqPool tracks job IDs.
        resp2 = _post_webhook(dup_client, payload)
        data2 = resp2.json()
        assert data2["status"] == "ignored"
        assert data2["reason"] == "job already queued"
        cooldown.reset()

    def test_different_prs_both_dispatch(self, client):
        """Different PR numbers get different job IDs — both dispatch."""
        resp1 = _post_webhook(client, _make_payload(pr_number=10))
        assert resp1.json()["status"] == "dispatched"

        resp2 = _post_webhook(
            client,
            _make_payload(pr_number=11, repo_full_name="other/repo"),
        )
        assert resp2.json()["status"] == "dispatched"
