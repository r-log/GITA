"""Tests for GithubClient.

Everything HTTP-shaped goes through ``httpx.MockTransport``. Zero network
I/O. The mock captures every request so tests can assert URLs, headers,
and bodies.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from gita.agents.decisions import Decision
from gita.github.client import GithubClient, _CachedToken


# ---------------------------------------------------------------------------
# Mock transport infrastructure
# ---------------------------------------------------------------------------
class RequestCapture:
    """Captures every request the client makes so tests can assert on them."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def record(self, request: httpx.Request) -> None:
        self.requests.append(request)


def _iso_in(seconds: int) -> str:
    ts = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_transport(
    handler, capture: RequestCapture
) -> httpx.MockTransport:
    def wrapped(request: httpx.Request) -> httpx.Response:
        capture.record(request)
        return handler(request)

    return httpx.MockTransport(wrapped)


def _default_handler(request: httpx.Request) -> httpx.Response:
    """Routes requests based on URL path to the expected response shape."""
    path = request.url.path
    if path.endswith("/installation"):
        return httpx.Response(
            200, json={"id": 999, "app_id": 123456}
        )
    if path.endswith("/access_tokens"):
        return httpx.Response(
            201,
            json={
                "token": "ghs_fake_installation_token",
                "expires_at": _iso_in(3600),
                "permissions": {"issues": "write"},
            },
        )
    if "/comments" in path:
        return httpx.Response(
            201,
            json={
                "id": 42,
                "html_url": "https://github.com/o/r/issues/7#issuecomment-42",
                "body": "hi",
            },
        )
    return httpx.Response(404, json={"message": "Unexpected URL"})


@pytest.fixture
def capture() -> RequestCapture:
    return RequestCapture()


@pytest.fixture
def mock_http(capture: RequestCapture) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_make_transport(_default_handler, capture),
        base_url="https://api.github.com",
    )


@pytest.fixture
def client(test_auth, mock_http) -> GithubClient:
    return GithubClient(auth=test_auth, http=mock_http)


def _comment_decision(
    repo: str = "owner/repo", issue: int = 7, body: str = "hi"
) -> Decision:
    return Decision(
        action="comment",
        target={"repo": repo, "issue": issue},
        payload={"body": body},
        evidence=["something"],
        confidence=0.9,
    )


# ---------------------------------------------------------------------------
# Installation lookup + token cache
# ---------------------------------------------------------------------------
class TestInstallationLookup:
    async def test_get_installation_id_hits_api(self, client, capture):
        installation_id = await client._get_installation_id("owner", "repo")
        assert installation_id == 999
        assert len(capture.requests) == 1
        req = capture.requests[0]
        assert req.method == "GET"
        assert req.url.path == "/repos/owner/repo/installation"
        assert req.headers["Accept"] == "application/vnd.github+json"
        assert req.headers["Authorization"].startswith("Bearer ")

    async def test_installation_lookup_is_cached(self, client, capture):
        await client._get_installation_id("owner", "repo")
        await client._get_installation_id("owner", "repo")
        install_requests = [
            r for r in capture.requests if r.url.path.endswith("/installation")
        ]
        assert len(install_requests) == 1


class TestInstallationTokenCache:
    async def test_fresh_token_comes_from_api(self, client, capture):
        token = await client._get_installation_token(999)
        assert token == "ghs_fake_installation_token"
        token_requests = [
            r for r in capture.requests if "/access_tokens" in r.url.path
        ]
        assert len(token_requests) == 1

    async def test_cached_token_skips_api(self, client, capture):
        """Second call within the freshness window should not hit the API."""
        await client._get_installation_token(999)
        await client._get_installation_token(999)
        token_requests = [
            r for r in capture.requests if "/access_tokens" in r.url.path
        ]
        assert len(token_requests) == 1

    async def test_expired_token_is_refreshed(self, client, capture):
        """If the cached token's expiry is within the safety window, refresh."""
        first = await client._get_installation_token(999)
        assert first == "ghs_fake_installation_token"

        # Force-expire the cached token
        client._installation_tokens[999] = _CachedToken(
            token="ghs_old",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        second = await client._get_installation_token(999)
        # Refreshed from the mock → fresh token value
        assert second == "ghs_fake_installation_token"
        token_requests = [
            r for r in capture.requests if "/access_tokens" in r.url.path
        ]
        assert len(token_requests) == 2


# ---------------------------------------------------------------------------
# execute(comment_decision)
# ---------------------------------------------------------------------------
class TestExecuteComment:
    async def test_posts_to_correct_url(self, client, capture):
        result = await client.execute(_comment_decision())
        assert result["kind"] == "comment"
        assert result["id"] == 42

        comment_requests = [
            r for r in capture.requests if "/comments" in r.url.path
        ]
        assert len(comment_requests) == 1
        req = comment_requests[0]
        assert req.method == "POST"
        assert req.url.path == "/repos/owner/repo/issues/7/comments"

    async def test_body_contains_comment_text(self, client, capture):
        await client.execute(_comment_decision(body="hello there"))
        comment_request = next(
            r for r in capture.requests if "/comments" in r.url.path
        )
        import json

        payload = json.loads(comment_request.content)
        assert payload == {"body": "hello there"}

    async def test_uses_installation_token_not_jwt_for_comment(
        self, client, capture
    ):
        """The comment POST should use `token <installation>`, not Bearer JWT.
        Bearer is only used against /app/* endpoints."""
        await client.execute(_comment_decision())
        comment_request = next(
            r for r in capture.requests if "/comments" in r.url.path
        )
        auth = comment_request.headers["Authorization"]
        assert auth.startswith("token ")
        assert "ghs_fake_installation_token" in auth

    async def test_flow_hits_three_endpoints_in_order(self, client, capture):
        """A cold call should hit installation lookup → token exchange →
        comment post, in that order."""
        await client.execute(_comment_decision())
        paths = [r.url.path for r in capture.requests]
        assert paths[0].endswith("/installation")
        assert "/access_tokens" in paths[1]
        assert paths[2].endswith("/comments")


# ---------------------------------------------------------------------------
# Validation and unsupported actions
# ---------------------------------------------------------------------------
class TestExecuteValidation:
    async def test_unsupported_action_raises(self, client):
        decision = Decision(
            action="create_issue",
            target={"repo": "a/b"},
            payload={"title": "x", "body": "y"},
            confidence=0.9,
        )
        with pytest.raises(NotImplementedError, match="create_issue"):
            await client.execute(decision)

    async def test_comment_missing_repo_raises(self, client):
        decision = Decision(
            action="comment",
            target={"issue": 1},  # missing repo
            payload={"body": "x"},
            confidence=0.9,
        )
        with pytest.raises(ValueError, match="target.repo"):
            await client.execute(decision)

    async def test_comment_missing_body_raises(self, client):
        decision = Decision(
            action="comment",
            target={"repo": "a/b", "issue": 1},
            payload={},  # no body
            confidence=0.9,
        )
        with pytest.raises(ValueError, match="payload.body"):
            await client.execute(decision)


# ---------------------------------------------------------------------------
# HTTP errors propagate cleanly
# ---------------------------------------------------------------------------
class TestHttpErrors:
    async def test_installation_404_raises(self, test_auth):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/installation"):
                return httpx.Response(
                    404, json={"message": "Not Found"}
                )
            return httpx.Response(200, json={})

        http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.github.com",
        )
        client = GithubClient(auth=test_auth, http=http)
        with pytest.raises(httpx.HTTPStatusError):
            await client.execute(_comment_decision())

    async def test_comment_500_raises(self, test_auth):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/installation"):
                return httpx.Response(200, json={"id": 999})
            if "/access_tokens" in request.url.path:
                return httpx.Response(
                    201,
                    json={
                        "token": "ghs_fake",
                        "expires_at": _iso_in(3600),
                    },
                )
            return httpx.Response(
                500, json={"message": "Internal Server Error"}
            )

        http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.github.com",
        )
        client = GithubClient(auth=test_auth, http=http)
        with pytest.raises(httpx.HTTPStatusError):
            await client.execute(_comment_decision())


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------
class TestActionClientProtocol:
    def test_github_client_is_an_action_client(self, test_auth):
        """Structural typing: GithubClient should satisfy ActionClient."""
        from gita.agents.decisions import ActionClient

        client: ActionClient = GithubClient(auth=test_auth)
        assert callable(client.execute)
