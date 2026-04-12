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
    method = request.method
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
    # POST /repos/o/r/issues — create_issue
    if method == "POST" and path.endswith("/issues"):
        return httpx.Response(
            201,
            json={
                "number": 777,
                "node_id": "I_kwDO",
                "html_url": "https://github.com/o/r/issues/777",
                "title": "ignored",
                "state": "open",
            },
        )
    # POST /repos/o/r/issues/N/labels — add_labels
    if method == "POST" and path.endswith("/labels"):
        return httpx.Response(
            200,
            json=[
                {"name": "bug"},
                {"name": "critical"},
            ],
        )
    # DELETE /repos/o/r/issues/N/labels/<label> — remove_label
    if method == "DELETE" and "/labels/" in path:
        return httpx.Response(
            200, json=[{"name": "other"}]
        )
    # PATCH /repos/o/r/issues/N — edit_issue or close_issue
    if method == "PATCH" and "/issues/" in path:
        return httpx.Response(
            200,
            json={
                "number": 7,
                "state": "closed",
                "html_url": "https://github.com/o/r/issues/7",
            },
        )
    # GET /repos/o/r/pulls/N — get_pr
    if method == "GET" and "/pulls/" in path and "/files" not in path:
        return httpx.Response(
            200,
            json={
                "number": 10,
                "title": "Fix SQL injection",
                "body": "This PR fixes the SQL injection in db.py",
                "state": "open",
                "user": {"login": "dev-alice"},
                "base": {"ref": "main"},
                "head": {"ref": "fix/sql-injection", "sha": "abc123def"},
                "changed_files": 3,
                "additions": 15,
                "deletions": 5,
                "html_url": "https://github.com/owner/repo/pull/10",
            },
        )
    # GET /repos/o/r/pulls/N/files — get_pr_files
    if method == "GET" and "/files" in path:
        return httpx.Response(
            200,
            json=[
                {
                    "sha": "abc123",
                    "filename": "src/db.py",
                    "status": "modified",
                    "additions": 10,
                    "deletions": 3,
                    "patch": (
                        "@@ -40,7 +40,14 @@ def get_user(user_id):\n"
                        " def get_user(user_id):\n"
                        "-    query = f\"SELECT * FROM users WHERE id={user_id}\"\n"
                        "+    query = \"SELECT * FROM users WHERE id=?\"\n"
                        "+    return execute(query, (user_id,))\n"
                    ),
                },
                {
                    "sha": "def456",
                    "filename": "src/auth.py",
                    "status": "modified",
                    "additions": 5,
                    "deletions": 2,
                    "patch": "@@ -10,5 +10,8 @@ def login():\n some context",
                },
            ],
        )
    return httpx.Response(404, json={"message": f"Unexpected {method} {path}"})


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
# execute(create_issue)
# ---------------------------------------------------------------------------
def _create_issue_decision(
    repo: str = "owner/repo",
    title: str = "Fix SQL injection",
    body: str = "details",
    labels: list[str] | None = None,
) -> Decision:
    payload: dict = {"title": title, "body": body}
    if labels is not None:
        payload["labels"] = labels
    return Decision(
        action="create_issue",
        target={"repo": repo},
        payload=payload,
        evidence=["e1"],
        confidence=0.9,
    )


class TestExecuteCreateIssue:
    async def test_posts_to_issues_endpoint(self, client, capture):
        result = await client.execute(_create_issue_decision())
        assert result["kind"] == "issue"
        assert result["id"] == 777
        assert (
            result["html_url"] == "https://github.com/o/r/issues/777"
        )

        create_requests = [
            r for r in capture.requests
            if r.method == "POST" and r.url.path.endswith("/issues")
        ]
        assert len(create_requests) == 1
        assert create_requests[0].url.path == "/repos/owner/repo/issues"

    async def test_body_has_title_body_and_labels(self, client, capture):
        await client.execute(
            _create_issue_decision(
                title="Fix bug", body="long body", labels=["bug", "critical"]
            )
        )
        req = next(
            r for r in capture.requests
            if r.method == "POST" and r.url.path.endswith("/issues")
        )
        import json

        payload = json.loads(req.content)
        assert payload["title"] == "Fix bug"
        assert payload["body"] == "long body"
        assert payload["labels"] == ["bug", "critical"]

    async def test_labels_omitted_if_empty(self, client, capture):
        """No labels key in the payload when none are provided, so the
        default-issue-creator flow doesn't accidentally send an empty list."""
        await client.execute(_create_issue_decision(labels=None))
        req = next(
            r for r in capture.requests
            if r.method == "POST" and r.url.path.endswith("/issues")
        )
        import json

        payload = json.loads(req.content)
        assert "labels" not in payload

    async def test_uses_installation_token(self, client, capture):
        await client.execute(_create_issue_decision())
        req = next(
            r for r in capture.requests
            if r.method == "POST" and r.url.path.endswith("/issues")
        )
        assert req.headers["Authorization"].startswith("token ")

    async def test_missing_title_raises(self, client):
        decision = Decision(
            action="create_issue",
            target={"repo": "a/b"},
            payload={"body": "x"},
            confidence=0.9,
        )
        with pytest.raises(ValueError, match="payload.title"):
            await client.execute(decision)


# ---------------------------------------------------------------------------
# execute(close_issue)
# ---------------------------------------------------------------------------
class TestExecuteCloseIssue:
    async def test_patches_correct_url_with_state_closed(
        self, client, capture
    ):
        decision = Decision(
            action="close_issue",
            target={"repo": "owner/repo", "issue": 7},
            payload={},
            confidence=0.9,
        )
        result = await client.execute(decision)
        assert result["kind"] == "close_issue"
        assert result["state"] == "closed"

        patch_requests = [
            r for r in capture.requests
            if r.method == "PATCH" and "/issues/" in r.url.path
        ]
        assert len(patch_requests) == 1
        req = patch_requests[0]
        assert req.url.path == "/repos/owner/repo/issues/7"

        import json

        payload = json.loads(req.content)
        assert payload == {"state": "closed"}
        assert req.headers["Authorization"].startswith("token ")

    async def test_missing_issue_raises(self, client):
        decision = Decision(
            action="close_issue",
            target={"repo": "a/b"},  # no issue
            payload={},
            confidence=0.9,
        )
        with pytest.raises(ValueError, match="target.issue"):
            await client.execute(decision)


# ---------------------------------------------------------------------------
# execute(edit_issue)
# ---------------------------------------------------------------------------
class TestExecuteEditIssue:
    async def test_patches_with_title_and_body(self, client, capture):
        decision = Decision(
            action="edit_issue",
            target={"repo": "owner/repo", "issue": 7},
            payload={"title": "New title", "body": "New body"},
            confidence=0.9,
        )
        result = await client.execute(decision)
        assert result["kind"] == "edit_issue"

        req = next(
            r for r in capture.requests
            if r.method == "PATCH" and "/issues/" in r.url.path
        )
        assert req.url.path == "/repos/owner/repo/issues/7"

        import json

        payload = json.loads(req.content)
        assert payload == {"title": "New title", "body": "New body"}

    async def test_partial_edit_sends_only_provided_fields(
        self, client, capture
    ):
        """Editing just the body shouldn't clobber the title with empty str."""
        decision = Decision(
            action="edit_issue",
            target={"repo": "owner/repo", "issue": 7},
            payload={"body": "only the body"},
            confidence=0.9,
        )
        await client.execute(decision)
        req = next(
            r for r in capture.requests
            if r.method == "PATCH" and "/issues/" in r.url.path
        )
        import json

        payload = json.loads(req.content)
        assert payload == {"body": "only the body"}
        assert "title" not in payload

    async def test_empty_payload_raises(self, client):
        decision = Decision(
            action="edit_issue",
            target={"repo": "a/b", "issue": 1},
            payload={},
            confidence=0.9,
        )
        with pytest.raises(ValueError, match="at least one of"):
            await client.execute(decision)


# ---------------------------------------------------------------------------
# execute(add_label)
# ---------------------------------------------------------------------------
class TestExecuteAddLabel:
    async def test_posts_to_labels_endpoint(self, client, capture):
        decision = Decision(
            action="add_label",
            target={"repo": "owner/repo", "issue": 7},
            payload={"labels": ["bug", "critical"]},
            confidence=0.9,
        )
        result = await client.execute(decision)
        assert result["kind"] == "add_label"
        assert result["labels"] == ["bug", "critical"]

        req = next(
            r for r in capture.requests
            if r.method == "POST" and r.url.path.endswith("/labels")
        )
        assert req.url.path == "/repos/owner/repo/issues/7/labels"

        import json

        payload = json.loads(req.content)
        assert payload == {"labels": ["bug", "critical"]}
        assert req.headers["Authorization"].startswith("token ")

    async def test_empty_labels_raises(self, client):
        decision = Decision(
            action="add_label",
            target={"repo": "a/b", "issue": 1},
            payload={"labels": []},
            confidence=0.9,
        )
        with pytest.raises(ValueError, match="non-empty payload.labels"):
            await client.execute(decision)


# ---------------------------------------------------------------------------
# execute(remove_label)
# ---------------------------------------------------------------------------
class TestExecuteRemoveLabel:
    async def test_delete_to_encoded_label_url(self, client, capture):
        decision = Decision(
            action="remove_label",
            target={"repo": "owner/repo", "issue": 7},
            payload={"label": "wont fix"},  # space triggers percent-encoding
            confidence=0.9,
        )
        result = await client.execute(decision)
        assert result["kind"] == "remove_label"
        assert result["label"] == "wont fix"

        req = next(
            r for r in capture.requests
            if r.method == "DELETE" and "/labels/" in r.url.path
        )
        # The wire-level path must carry the percent-encoded label.
        # httpx decodes `.path` back to a readable form but preserves the
        # encoded bytes in `raw_path`.
        assert (
            req.url.raw_path
            == b"/repos/owner/repo/issues/7/labels/wont%20fix"
        )
        assert req.headers["Authorization"].startswith("token ")

    async def test_missing_label_raises(self, client):
        decision = Decision(
            action="remove_label",
            target={"repo": "a/b", "issue": 1},
            payload={},
            confidence=0.9,
        )
        with pytest.raises(ValueError, match="payload.label"):
            await client.execute(decision)


# ---------------------------------------------------------------------------
# Validation and unsupported actions
# ---------------------------------------------------------------------------
class TestExecuteValidation:
    async def test_unsupported_action_raises(self, client):
        decision = Decision(
            action="merge_pr",  # hypothetical future action, not wired
            target={"repo": "a/b"},
            payload={},
            confidence=0.9,
        )
        with pytest.raises(NotImplementedError, match="merge_pr"):
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
# PR reading (not through execute() — direct methods)
# ---------------------------------------------------------------------------
class TestGetPr:
    async def test_returns_pr_info(self, client, capture):
        pr = await client.get_pr("owner", "repo", 10)
        assert pr.number == 10
        assert pr.title == "Fix SQL injection"
        assert pr.author == "dev-alice"
        assert pr.state == "open"
        assert pr.base_ref == "main"
        assert pr.head_ref == "fix/sql-injection"
        assert pr.head_sha == "abc123def"
        assert pr.changed_files == 3
        assert pr.additions == 15
        assert pr.deletions == 5
        assert pr.html_url == "https://github.com/owner/repo/pull/10"

    async def test_hits_correct_url(self, client, capture):
        await client.get_pr("owner", "repo", 10)
        pr_requests = [
            r for r in capture.requests
            if "/pulls/" in r.url.path and "/files" not in r.url.path
        ]
        assert len(pr_requests) == 1
        assert pr_requests[0].url.path == "/repos/owner/repo/pulls/10"
        assert pr_requests[0].method == "GET"

    async def test_uses_installation_token(self, client, capture):
        await client.get_pr("owner", "repo", 10)
        pr_req = next(
            r for r in capture.requests
            if "/pulls/" in r.url.path and "/files" not in r.url.path
        )
        assert pr_req.headers["Authorization"].startswith("token ")


class TestGetPrFiles:
    async def test_returns_file_list(self, client, capture):
        files = await client.get_pr_files("owner", "repo", 10)
        assert isinstance(files, list)
        assert len(files) == 2
        assert files[0]["filename"] == "src/db.py"
        assert files[1]["filename"] == "src/auth.py"

    async def test_hits_correct_url_with_pagination(self, client, capture):
        await client.get_pr_files("owner", "repo", 10)
        file_requests = [
            r for r in capture.requests if "/files" in r.url.path
        ]
        assert len(file_requests) == 1
        assert "/pulls/10/files" in file_requests[0].url.path

    async def test_files_have_patch_field(self, client, capture):
        files = await client.get_pr_files("owner", "repo", 10)
        assert files[0]["patch"] is not None
        assert "@@ " in files[0]["patch"]


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------
class TestActionClientProtocol:
    def test_github_client_is_an_action_client(self, test_auth):
        """Structural typing: GithubClient should satisfy ActionClient."""
        from gita.agents.decisions import ActionClient

        client: ActionClient = GithubClient(auth=test_auth)
        assert callable(client.execute)
