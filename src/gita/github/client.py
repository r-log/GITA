"""Minimal GitHub App client.

Week 2 shipped ``comment`` only. Week 3 expands the dispatch to cover the
write path onboarding needs: ``create_issue``, ``close_issue``,
``edit_issue``, ``add_label``, ``remove_label``. Every other action still
raises ``NotImplementedError`` so unknown dispatch shapes fail loud.

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

import base64
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

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

        Validation is per-action — each branch extracts the fields it
        needs and raises ``ValueError`` with a pointer to the offending
        target/payload if they're wrong. Unknown actions raise
        ``NotImplementedError`` so the decision framework surfaces a
        clear error rather than silently doing nothing.
        """
        action = decision.action
        repo_full_name = decision.target.get("repo")
        if not repo_full_name:
            raise ValueError(
                f"{action} decision must have target.repo; "
                f"got {decision.target=}"
            )
        owner, repo = str(repo_full_name).split("/", 1)

        if action == "comment":
            issue = decision.target.get("issue")
            body = decision.payload.get("body")
            if issue is None or not body:
                raise ValueError(
                    "comment decision must have target.issue and payload.body; "
                    f"got {decision.target=} {decision.payload=}"
                )
            return await self._post_comment(owner, repo, int(issue), str(body))

        if action == "create_issue":
            title = decision.payload.get("title")
            if not title:
                raise ValueError(
                    "create_issue decision must have payload.title; "
                    f"got {decision.payload=}"
                )
            body = decision.payload.get("body") or ""
            labels = list(decision.payload.get("labels") or [])
            return await self._create_issue(
                owner, repo, str(title), str(body), labels
            )

        if action == "close_issue":
            issue = decision.target.get("issue")
            if issue is None:
                raise ValueError(
                    "close_issue decision must have target.issue; "
                    f"got {decision.target=}"
                )
            return await self._close_issue(owner, repo, int(issue))

        if action == "edit_issue":
            issue = decision.target.get("issue")
            if issue is None:
                raise ValueError(
                    "edit_issue decision must have target.issue; "
                    f"got {decision.target=}"
                )
            title = decision.payload.get("title")
            body = decision.payload.get("body")
            if title is None and body is None:
                raise ValueError(
                    "edit_issue decision must include at least one of "
                    "payload.title or payload.body; "
                    f"got {decision.payload=}"
                )
            return await self._edit_issue(
                owner,
                repo,
                int(issue),
                title=str(title) if title is not None else None,
                body=str(body) if body is not None else None,
            )

        if action == "add_label":
            issue = decision.target.get("issue")
            labels = decision.payload.get("labels") or []
            if issue is None or not labels:
                raise ValueError(
                    "add_label decision must have target.issue and a "
                    "non-empty payload.labels list; "
                    f"got {decision.target=} {decision.payload=}"
                )
            return await self._add_labels(
                owner, repo, int(issue), [str(label) for label in labels]
            )

        if action == "remove_label":
            issue = decision.target.get("issue")
            label = decision.payload.get("label")
            if issue is None or not label:
                raise ValueError(
                    "remove_label decision must have target.issue and "
                    "payload.label; "
                    f"got {decision.target=} {decision.payload=}"
                )
            return await self._remove_label(
                owner, repo, int(issue), str(label)
            )

        if action == "create_branch":
            ref_name = decision.payload.get("ref")
            base_sha = decision.payload.get("base_sha")
            if not ref_name or not base_sha:
                raise ValueError(
                    "create_branch decision must have payload.ref and "
                    "payload.base_sha; "
                    f"got {decision.payload=}"
                )
            return await self._create_ref(
                owner, repo, str(ref_name), str(base_sha)
            )

        if action == "update_file":
            path = decision.payload.get("path")
            content = decision.payload.get("content")
            message = decision.payload.get("message")
            branch = decision.payload.get("branch")
            if (
                not path
                or content is None
                or not message
                or not branch
            ):
                raise ValueError(
                    "update_file decision must have payload.path, "
                    "payload.content, payload.message, payload.branch; "
                    f"got {decision.payload=}"
                )
            sha = decision.payload.get("sha")
            return await self._create_or_update_file(
                owner,
                repo,
                str(path),
                str(message),
                str(content),
                str(branch),
                sha=str(sha) if sha is not None else None,
            )

        if action == "open_pr":
            title = decision.payload.get("title")
            body = decision.payload.get("body") or ""
            head = decision.payload.get("head")
            base = decision.payload.get("base")
            if not title or not head or not base:
                raise ValueError(
                    "open_pr decision must have payload.title, "
                    "payload.head, payload.base; "
                    f"got {decision.payload=}"
                )
            draft = bool(decision.payload.get("draft", False))
            return await self._create_pull(
                owner,
                repo,
                str(title),
                str(body),
                str(head),
                str(base),
                draft=draft,
            )

        raise NotImplementedError(
            f"GithubClient does not support action {action!r}"
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
    # Action handlers
    # -----------------------------------------------------------------
    def _repo_auth_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"token {token}",
            "Accept": _ACCEPT,
            "X-GitHub-Api-Version": _API_VERSION,
        }

    async def _post_comment(
        self, owner: str, repo: str, issue: int, body: str
    ) -> dict[str, Any]:
        token = await self._installation_token_for_repo(owner, repo)
        url = f"{_GITHUB_API}/repos/{owner}/{repo}/issues/{issue}/comments"
        response = await self.http.post(
            url,
            headers=self._repo_auth_headers(token),
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

    async def _create_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        labels: list[str],
    ) -> dict[str, Any]:
        token = await self._installation_token_for_repo(owner, repo)
        url = f"{_GITHUB_API}/repos/{owner}/{repo}/issues"
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        response = await self.http.post(
            url,
            headers=self._repo_auth_headers(token),
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        logger.info(
            "github_issue_created owner=%s repo=%s issue=%s labels=%s",
            owner,
            repo,
            data.get("number"),
            labels,
        )
        return {
            "kind": "issue",
            "id": data.get("number"),
            "node_id": data.get("node_id"),
            "html_url": data.get("html_url"),
            "repo": f"{owner}/{repo}",
            "title": title,
        }

    async def _close_issue(
        self, owner: str, repo: str, issue: int
    ) -> dict[str, Any]:
        token = await self._installation_token_for_repo(owner, repo)
        url = f"{_GITHUB_API}/repos/{owner}/{repo}/issues/{issue}"
        response = await self.http.patch(
            url,
            headers=self._repo_auth_headers(token),
            json={"state": "closed"},
        )
        response.raise_for_status()
        data = response.json()
        logger.info(
            "github_issue_closed owner=%s repo=%s issue=%s state=%s",
            owner,
            repo,
            issue,
            data.get("state"),
        )
        return {
            "kind": "close_issue",
            "id": data.get("number", issue),
            "html_url": data.get("html_url"),
            "state": data.get("state"),
            "repo": f"{owner}/{repo}",
        }

    async def _edit_issue(
        self,
        owner: str,
        repo: str,
        issue: int,
        *,
        title: str | None,
        body: str | None,
    ) -> dict[str, Any]:
        token = await self._installation_token_for_repo(owner, repo)
        url = f"{_GITHUB_API}/repos/{owner}/{repo}/issues/{issue}"
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        response = await self.http.patch(
            url,
            headers=self._repo_auth_headers(token),
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        logger.info(
            "github_issue_edited owner=%s repo=%s issue=%s fields=%s",
            owner,
            repo,
            issue,
            sorted(payload.keys()),
        )
        return {
            "kind": "edit_issue",
            "id": data.get("number", issue),
            "html_url": data.get("html_url"),
            "repo": f"{owner}/{repo}",
        }

    async def _add_labels(
        self,
        owner: str,
        repo: str,
        issue: int,
        labels: list[str],
    ) -> dict[str, Any]:
        token = await self._installation_token_for_repo(owner, repo)
        url = (
            f"{_GITHUB_API}/repos/{owner}/{repo}/issues/{issue}/labels"
        )
        response = await self.http.post(
            url,
            headers=self._repo_auth_headers(token),
            json={"labels": labels},
        )
        response.raise_for_status()
        data = response.json()
        applied = (
            [row.get("name") for row in data] if isinstance(data, list) else []
        )
        logger.info(
            "github_labels_added owner=%s repo=%s issue=%s labels=%s applied=%s",
            owner,
            repo,
            issue,
            labels,
            applied,
        )
        return {
            "kind": "add_label",
            "id": issue,
            "repo": f"{owner}/{repo}",
            "labels": applied,
        }

    async def _remove_label(
        self,
        owner: str,
        repo: str,
        issue: int,
        label: str,
    ) -> dict[str, Any]:
        token = await self._installation_token_for_repo(owner, repo)
        encoded_label = quote(label, safe="")
        url = (
            f"{_GITHUB_API}/repos/{owner}/{repo}/issues/{issue}/labels/"
            f"{encoded_label}"
        )
        response = await self.http.delete(
            url,
            headers=self._repo_auth_headers(token),
        )
        response.raise_for_status()
        logger.info(
            "github_label_removed owner=%s repo=%s issue=%s label=%s",
            owner,
            repo,
            issue,
            label,
        )
        return {
            "kind": "remove_label",
            "id": issue,
            "repo": f"{owner}/{repo}",
            "label": label,
        }

    # -----------------------------------------------------------------
    # Contents API write helpers
    #
    # Private — dispatched by ``execute()`` in Week 8 Day 2 when the
    # corresponding action types (``create_branch``, ``update_file``,
    # ``open_pr``) land in the Decision framework.
    # -----------------------------------------------------------------
    async def _create_ref(
        self,
        owner: str,
        repo: str,
        ref_name: str,
        sha: str,
    ) -> dict[str, Any]:
        """Create a git ref (typically a branch).

        ``ref_name`` is the full ref path (e.g. ``"refs/heads/feature"``).
        Returns the branch ref GitHub actually stored + the source SHA
        so callers can log/audit the exact branch point.
        """
        token = await self._installation_token_for_repo(owner, repo)
        url = f"{_GITHUB_API}/repos/{owner}/{repo}/git/refs"
        response = await self.http.post(
            url,
            headers=self._repo_auth_headers(token),
            json={"ref": ref_name, "sha": sha},
        )
        response.raise_for_status()
        data = response.json()
        logger.info(
            "github_ref_created owner=%s repo=%s ref=%s sha=%s",
            owner,
            repo,
            ref_name,
            sha,
        )
        return {
            "kind": "create_branch",
            "ref": data.get("ref"),
            "sha": data.get("object", {}).get("sha"),
            "url": data.get("url"),
            "repo": f"{owner}/{repo}",
        }

    async def _create_or_update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        message: str,
        content: str,
        branch: str,
        sha: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a file via the Contents API.

        ``content`` is the raw UTF-8 string; GitHub expects base64, so
        we encode here. When ``sha`` is supplied this is an update
        (GitHub requires the existing blob SHA to prevent lost-update
        races); when omitted it's a create.
        """
        token = await self._installation_token_for_repo(owner, repo)
        encoded_path = quote(path, safe="/")
        url = (
            f"{_GITHUB_API}/repos/{owner}/{repo}/contents/{encoded_path}"
        )
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode(
                "ascii"
            ),
            "branch": branch,
        }
        if sha is not None:
            payload["sha"] = sha
        response = await self.http.put(
            url,
            headers=self._repo_auth_headers(token),
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        content_data = data.get("content", {}) or {}
        commit_data = data.get("commit", {}) or {}
        logger.info(
            "github_file_written owner=%s repo=%s path=%s branch=%s "
            "mode=%s commit=%s",
            owner,
            repo,
            path,
            branch,
            "update" if sha is not None else "create",
            commit_data.get("sha"),
        )
        return {
            "kind": "update_file" if sha is not None else "create_file",
            "path": content_data.get("path", path),
            "content_sha": content_data.get("sha"),
            "commit_sha": commit_data.get("sha"),
            "html_url": content_data.get("html_url"),
            "branch": branch,
            "repo": f"{owner}/{repo}",
        }

    async def _create_pull(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
    ) -> dict[str, Any]:
        """Open a pull request from ``head`` into ``base``."""
        token = await self._installation_token_for_repo(owner, repo)
        url = f"{_GITHUB_API}/repos/{owner}/{repo}/pulls"
        payload: dict[str, Any] = {
            "title": title,
            "body": body,
            "head": head,
            "base": base,
        }
        if draft:
            payload["draft"] = True
        response = await self.http.post(
            url,
            headers=self._repo_auth_headers(token),
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        logger.info(
            "github_pr_opened owner=%s repo=%s pr=%s head=%s base=%s "
            "draft=%s",
            owner,
            repo,
            data.get("number"),
            head,
            base,
            draft,
        )
        return {
            "kind": "open_pr",
            "number": data.get("number"),
            "html_url": data.get("html_url"),
            "head": head,
            "base": base,
            "state": data.get("state"),
            "repo": f"{owner}/{repo}",
        }

    # -----------------------------------------------------------------
    # PR reading (not part of ActionClient — called directly by agents)
    # -----------------------------------------------------------------
    async def get_pr(
        self, owner: str, repo: str, number: int
    ) -> "PRInfo":
        """Fetch PR metadata. Returns a typed dataclass, not raw JSON."""
        token = await self._installation_token_for_repo(owner, repo)
        url = f"{_GITHUB_API}/repos/{owner}/{repo}/pulls/{number}"
        response = await self.http.get(
            url,
            headers=self._repo_auth_headers(token),
        )
        response.raise_for_status()
        data = response.json()
        return PRInfo(
            number=data["number"],
            title=data.get("title", ""),
            body=data.get("body") or "",
            author=data.get("user", {}).get("login", ""),
            state=data.get("state", "open"),
            base_ref=data.get("base", {}).get("ref", ""),
            head_ref=data.get("head", {}).get("ref", ""),
            head_sha=data.get("head", {}).get("sha", ""),
            changed_files=data.get("changed_files", 0),
            additions=data.get("additions", 0),
            deletions=data.get("deletions", 0),
            html_url=data.get("html_url", ""),
        )

    async def get_pr_files(
        self, owner: str, repo: str, number: int
    ) -> list[dict[str, Any]]:
        """Fetch the list of files changed in a PR.

        Returns the raw JSON array from GitHub's
        ``GET /repos/{owner}/{repo}/pulls/{n}/files`` endpoint. The
        caller (typically ``diff_parser.parse_pr_files``) normalizes
        this into typed ``DiffHunk`` objects.

        Paginates automatically — GitHub caps each page at 30 files by
        default; PRs with 100+ changed files need multiple pages.
        """
        token = await self._installation_token_for_repo(owner, repo)
        all_files: list[dict[str, Any]] = []
        page = 1
        while True:
            url = (
                f"{_GITHUB_API}/repos/{owner}/{repo}/pulls/{number}/files"
                f"?per_page=100&page={page}"
            )
            response = await self.http.get(
                url,
                headers=self._repo_auth_headers(token),
            )
            response.raise_for_status()
            batch = response.json()
            if not batch:
                break
            all_files.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        logger.info(
            "github_pr_files_fetched owner=%s repo=%s pr=%d files=%d pages=%d",
            owner,
            repo,
            number,
            len(all_files),
            page,
        )
        return all_files

    # -----------------------------------------------------------------
    # Contents API read methods (called directly by agents during recipe
    # execution — not part of the ActionClient write path)
    # -----------------------------------------------------------------
    async def get_ref(
        self, owner: str, repo: str, ref: str
    ) -> "RefInfo":
        """Fetch a git ref (branch or tag).

        ``ref`` is the short ref (e.g. ``"heads/main"``), NOT the fully
        qualified ``"refs/heads/main"`` — this matches GitHub's
        ``GET /git/ref/{ref}`` convention.
        """
        token = await self._installation_token_for_repo(owner, repo)
        encoded_ref = quote(ref, safe="/")
        url = (
            f"{_GITHUB_API}/repos/{owner}/{repo}/git/ref/{encoded_ref}"
        )
        response = await self.http.get(
            url,
            headers=self._repo_auth_headers(token),
        )
        response.raise_for_status()
        data = response.json()
        return RefInfo(
            ref=data.get("ref", ""),
            sha=data.get("object", {}).get("sha", ""),
            url=data.get("url", ""),
        )

    async def get_contents(
        self,
        owner: str,
        repo: str,
        path: str,
        *,
        ref: str | None = None,
    ) -> "FileContents":
        """Fetch a file's contents from the repo.

        Returns a ``FileContents`` with base64-decoded UTF-8 content and
        the blob SHA needed for update-file calls. Only single-file
        reads are supported here — directory reads return a JSON array
        and raise ``ValueError``. Callers always ask for a specific
        file path in the test-generation flow.
        """
        token = await self._installation_token_for_repo(owner, repo)
        encoded_path = quote(path, safe="/")
        url = (
            f"{_GITHUB_API}/repos/{owner}/{repo}/contents/{encoded_path}"
        )
        if ref is not None:
            url += f"?ref={quote(ref, safe='')}"
        response = await self.http.get(
            url,
            headers=self._repo_auth_headers(token),
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            raise ValueError(
                f"get_contents(path={path!r}) returned a directory "
                "listing; pass a file path, not a directory"
            )
        encoding = data.get("encoding", "base64")
        raw_content = data.get("content", "") or ""
        if encoding == "base64":
            decoded = base64.b64decode(
                raw_content.replace("\n", "")
            ).decode("utf-8")
        else:
            decoded = raw_content
        return FileContents(
            path=data.get("path", path),
            content=decoded,
            sha=data.get("sha", ""),
            encoding=encoding,
            size=int(data.get("size", 0)),
        )


# ---------------------------------------------------------------------------
# PR data types (outside the class so they're importable without a client)
# ---------------------------------------------------------------------------
@dataclass
class PRInfo:
    """Typed representation of a GitHub pull request's metadata."""

    number: int
    title: str
    body: str
    author: str
    state: str  # "open" | "closed" | "merged"
    base_ref: str
    head_ref: str
    head_sha: str
    changed_files: int
    additions: int
    deletions: int
    html_url: str


@dataclass
class RefInfo:
    """Typed representation of a git ref (branch or tag)."""

    ref: str  # e.g. "refs/heads/main"
    sha: str  # commit SHA the ref points at
    url: str  # GitHub API URL for the ref


@dataclass
class FileContents:
    """Typed representation of a file fetched from a repo.

    ``content`` is already decoded from base64 into a UTF-8 string.
    ``sha`` is the blob SHA — required when calling
    ``_create_or_update_file`` with ``sha=`` to update this file.
    """

    path: str
    content: str
    sha: str
    encoding: str
    size: int
