"""
Root conftest.py — shared fixtures for the entire test suite.

IMPORTANT: Environment variables are set at module level (before any src imports)
to ensure the Settings singleton in src.core.config can be constructed without a .env file.
"""

import os

# --- Set env vars BEFORE any src imports (Settings() runs at import time) ---
os.environ.setdefault("GITHUB_APP_ID", "12345")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("OPENROUTER_API_KEY", "test-openrouter-key")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test_db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("GITHUB_APP_PRIVATE_KEY_PATH", "tests/fixtures/fake-key.pem")
os.environ.setdefault("ENVIRONMENT", "testing")
os.environ.setdefault("LOG_LEVEL", "WARNING")

import hashlib
import hmac
import json
from dataclasses import field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.base import AgentContext, AgentResult
from src.tools.base import Tool, ToolResult


# ---------------------------------------------------------------------------
# Factory helpers — create common test data
# ---------------------------------------------------------------------------

def make_agent_context(
    event_type: str = "issues.opened",
    event_payload: dict | None = None,
    repo_full_name: str = "test-owner/test-repo",
    installation_id: int = 1001,
    repo_id: int = 42,
    additional_data: dict | None = None,
) -> AgentContext:
    """Create an AgentContext with sensible defaults."""
    return AgentContext(
        event_type=event_type,
        event_payload=event_payload or {},
        repo_full_name=repo_full_name,
        installation_id=installation_id,
        repo_id=repo_id,
        additional_data=additional_data or {},
    )


def make_agent_result(
    agent_name: str = "test_agent",
    status: str = "success",
    **kwargs,
) -> AgentResult:
    """Create an AgentResult with sensible defaults."""
    return AgentResult(agent_name=agent_name, status=status, **kwargs)


def make_issue_data(
    number: int = 1,
    title: str = "Test issue",
    state: str = "open",
    body: str = "",
    labels: list[dict] | None = None,
    milestone: dict | None = None,
    user: dict | None = None,
    pull_request: dict | None = None,
) -> dict:
    """Create a GitHub issue API response dict."""
    data = {
        "number": number,
        "title": title,
        "state": state,
        "body": body,
        "labels": labels or [],
        "milestone": milestone,
        "user": user or {"login": "test-user", "id": 100},
        "html_url": f"https://github.com/test-owner/test-repo/issues/{number}",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    if pull_request is not None:
        data["pull_request"] = pull_request
    return data


def make_pr_data(
    number: int = 10,
    title: str = "Test PR",
    state: str = "open",
    body: str = "",
    user: dict | None = None,
    head: dict | None = None,
    base: dict | None = None,
) -> dict:
    """Create a GitHub pull request API response dict."""
    return {
        "number": number,
        "title": title,
        "state": state,
        "body": body,
        "user": user or {"login": "test-user", "id": 100},
        "head": head or {"ref": "feature-branch", "sha": "abc123"},
        "base": base or {"ref": "main", "sha": "def456"},
        "html_url": f"https://github.com/test-owner/test-repo/pull/{number}",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def make_webhook_payload(
    event_type: str = "issues",
    action: str = "opened",
    repo_full_name: str = "test-owner/test-repo",
    installation_id: int = 1001,
    sender: dict | None = None,
    extra: dict | None = None,
) -> dict:
    """Create a GitHub webhook payload dict."""
    payload = {
        "action": action,
        "repository": {
            "full_name": repo_full_name,
            "id": 999,
        },
        "installation": {"id": installation_id},
        "sender": sender or {"login": "test-user", "type": "User", "id": 100},
    }
    if extra:
        payload.update(extra)
    return payload


def compute_signature(body: bytes, secret: str = "test-webhook-secret") -> str:
    """Compute the X-Hub-Signature-256 header value for a webhook body."""
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


# ---------------------------------------------------------------------------
# Mock GitHub client
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_github_client():
    """
    Factory fixture that creates a mock GitHubClient.

    Usage:
        client = mock_github_client(get_responses={"/repos/...": {"id": 1}})
    """
    def _factory(
        get_responses: dict[str, Any] | None = None,
        post_responses: dict[str, Any] | None = None,
        patch_responses: dict[str, Any] | None = None,
        default_response: dict | None = None,
    ):
        client = AsyncMock()
        client.installation_id = 1001
        _default = default_response or {}

        async def _mock_get(endpoint, **kwargs):
            if get_responses and endpoint in get_responses:
                return get_responses[endpoint]
            return _default

        async def _mock_post(endpoint, **kwargs):
            if post_responses and endpoint in post_responses:
                return post_responses[endpoint]
            return _default

        async def _mock_patch(endpoint, **kwargs):
            if patch_responses and endpoint in patch_responses:
                return patch_responses[endpoint]
            return _default

        client.get = AsyncMock(side_effect=_mock_get)
        client.post = AsyncMock(side_effect=_mock_post)
        client.patch = AsyncMock(side_effect=_mock_patch)
        client._ensure_token = AsyncMock(return_value="fake-token")
        client.request = AsyncMock()
        return client

    return _factory


# ---------------------------------------------------------------------------
# Mock OpenAI / OpenRouter client
# ---------------------------------------------------------------------------

def make_llm_response(
    content: str = "LLM response",
    tool_calls: list | None = None,
    finish_reason: str = "stop",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
):
    """Create a mock OpenAI chat completion response."""
    message = MagicMock()
    message.content = content
    message.tool_calls = tool_calls
    message.model_dump.return_value = {
        "role": "assistant",
        "content": content,
        "tool_calls": [tc if isinstance(tc, dict) else tc.model_dump() for tc in tool_calls] if tool_calls else None,
    }

    choice = MagicMock()
    choice.message = message
    choice.finish_reason = finish_reason

    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens

    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    return response


def make_tool_call(
    name: str,
    arguments: dict | None = None,
    call_id: str = "call_001",
):
    """Create a mock tool call object (as returned by OpenAI API)."""
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = name
    tc.function.arguments = json.dumps(arguments or {})
    tc.model_dump.return_value = {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments or {})},
    }
    return tc


@pytest.fixture
def mock_openai_client():
    """Returns a mock AsyncOpenAI client with configurable responses."""
    client = AsyncMock()

    def set_responses(*responses):
        """Set a sequence of responses for successive LLM calls."""
        client.chat.completions.create = AsyncMock(side_effect=list(responses))

    client.set_responses = set_responses
    # Default: single text response
    client.chat.completions.create = AsyncMock(
        return_value=make_llm_response(content="Default LLM response")
    )
    return client


# ---------------------------------------------------------------------------
# Mock database session
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db_session():
    """
    Patches src.core.database.async_session to return a mock session.
    Returns the mock session for assertions.
    """
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(
        scalar_one_or_none=MagicMock(return_value=None),
        scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
    ))
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    session.add = MagicMock()
    session.refresh = AsyncMock()

    # Make it work as an async context manager
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=ctx)

    with patch("src.core.database.async_session", mock_factory):
        yield session


# ---------------------------------------------------------------------------
# Mock Redis
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_redis():
    """Returns a mock Redis client."""
    r = AsyncMock()
    r.set = AsyncMock(return_value=True)  # set-if-not-exists succeeds
    r.get = AsyncMock(return_value=None)
    r.delete = AsyncMock()
    r.ping = AsyncMock()
    return r


# ---------------------------------------------------------------------------
# Simple tool factory for agent tests
# ---------------------------------------------------------------------------

def make_tool(
    name: str = "test_tool",
    description: str = "A test tool",
    parameters: dict | None = None,
    result: ToolResult | None = None,
) -> Tool:
    """Create a Tool with a mock handler that returns the given result."""
    _result = result or ToolResult(success=True, data={"ok": True})

    async def _handler(**kwargs):
        return _result

    return Tool(
        name=name,
        description=description,
        parameters=parameters or {"type": "object", "properties": {}},
        handler=_handler,
    )
