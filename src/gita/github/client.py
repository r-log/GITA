"""Minimal GitHub App client.

Scope for Week 2: **one action only** — posting a comment on an issue. The
``ActionClient`` protocol from ``gita.agents.decisions`` lets the decision
framework route ``action="comment"`` decisions through this client. Every
other action raises ``NotImplementedError`` until Week 3 wires issue CRUD.

Architecture:
- ``GithubClient`` is instantiated once per process and reused.
- It owns its own ``httpx.AsyncClient`` by default (so callers don't have
  to manage one), but accepts an injected client for testing with
  ``httpx.MockTransport``.
- Installation lookup (``(owner, repo) → installation_id``) is cached
  indefinitely because installation assignments rarely change.
- Installation tokens are cached per installation with a 5-minute safety
  window before their real expiry.
- Every HTTP call uses a fresh JWT (cheap to sign, safer than caching).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from gita.agents.decisions import Decision
from gita.github.auth import GithubAppAuth

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"
_USER_AGENT = "gita/0.1.0"

# Refresh installation tokens this many seconds before their real expiry
# to avoid racing the clock on a slow API call.
_TOKEN_REFRESH_SKEW = timedelta(minutes=5)


@dataclass
class _CachedToken:
    token: str
    expires_at: datetime  # tz-aware UTC

    def is_fresh(self, now: datetime) -> bool:
        return self.expires_at - _TOKEN_REFRESH_SKEW > now


class GithubClient:
    """Implements the :class:`~gita.agents.decisions.ActionClient` protocol."""

    def __init__(
        self,
        auth: GithubAppAuth,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.auth = auth
        self._owns_http = http is None
        self.http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers={"User-Agent": _USER_AGENT},
        )
        self._installation_ids: dict[tuple[str, str], int] = {}
        self._installation_tokens: dict[int, _CachedToken] = {}

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------
    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    async def __aenter__(self) -> "GithubClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # -----------------------------------------------------------------
    # ActionClient protocol
    # -----------------------------------------------------------------
    async def execute(self, decision: Decision) -> dict[str, Any]:
        """Dispatch a decision to its matching GitHub API call.

        Week 2 only supports ``action="comment"``. Any other action raises
        ``NotImplementedError`` so the decision framework surfaces a clear
        error rather than silently doing nothing.
        """
        if decision.action == "comment":
            repo_full_name = decision.target.get("repo")
            issue = decision.target.get("issue")
            body = decision.payload.get("body")
            if not repo_full_name or issue is None or not body:
                raise ValueError(
                    "comment decision must have target.repo, target.issue, "
                    f"and payload.body; got {decision.target=} {decision.payload=}"
                )
            owner, repo = str(repo_full_name).split("/", 1)
            return await self._post_comment(owner, repo, int(issue), str(body))

        raise NotImplementedError(
            f"GithubClient does not support action {decision.action!r} yet "
            "(Week 2 ships comments only)"
        )

    # -----------------------------------------------------------------
    # Installation resolution + token cache
    # -----------------------------------------------------------------
    async def _get_installation_id(self, owner: str, repo: str) -> int:
        key = (owner, repo)
        cached_id = self._installation_ids.get(key)
        if cached_id is not None:
            return cached_id

        jwt_token = self.auth.generate_jwt()
        response = await self.http.get(
            f"{_GITHUB_API}/repos/{owner}/{repo}/installation",
            headers={
                "Authorization": f"Bearer {jwt_token}",
                "Accept": _ACCEPT,
                "X-GitHub-Api-Version": _API_VERSION,
            },
        )
        response.raise_for_status()
        data = response.json()
        installation_id = int(data["id"])
        self._installation_ids[key] = installation_id
        return installation_id

    async def _get_installation_token(
        self, installation_id: int, *, now: datetime | None = None
    ) -> str:
        current = now if now is not None else datetime.now(timezone.utc)
        cached = self._installation_tokens.get(installation_id)
        if cached is not None and cached.is_fresh(current):
            return cached.token

        jwt_token = self.auth.generate_jwt()
        response = await self.http.post(
            f"{_GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {jwt_token}",
                "Accept": _ACCEPT,
                "X-GitHub-Api-Version": _API_VERSION,
            },
        )
        response.raise_for_status()
        data = response.json()
        expires_at = datetime.fromisoformat(
            data["expires_at"].replace("Z", "+00:00")
        )
        token = data["token"]
        self._installation_tokens[installation_id] = _CachedToken(
            token=token, expires_at=expires_at
        )
        return token

    async def _installation_token_for_repo(
        self, owner: str, repo: str
    ) -> str:
        installation_id = await self._get_installation_id(owner, repo)
        return await self._get_installation_token(installation_id)

    # -----------------------------------------------------------------
    # Comment posting
    # -----------------------------------------------------------------
    async def _post_comment(
        self, owner: str, repo: str, issue: int, body: str
    ) -> dict[str, Any]:
        token = await self._installation_token_for_repo(owner, repo)
        url = (
            f"{_GITHUB_API}/repos/{owner}/{repo}/issues/{issue}/comments"
        )
        response = await self.http.post(
            url,
            headers={
                "Authorization": f"token {token}",
                "Accept": _ACCEPT,
                "X-GitHub-Api-Version": _API_VERSION,
            },
            json={"body": body},
        )
        response.raise_for_status()
        data = response.json()
        logger.info(
            "github_comment_posted owner=%s repo=%s issue=%s comment_id=%s",
            owner,
            repo,
            issue,
            data.get("id"),
        )
        return {
            "kind": "comment",
            "id": data.get("id"),
            "html_url": data.get("html_url"),
            "issue": issue,
            "repo": f"{owner}/{repo}",
        }
