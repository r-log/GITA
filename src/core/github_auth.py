"""
GitHub App authentication.
Handles JWT generation and installation access token management.
"""

import time
from typing import Optional

import httpx
import jwt

from src.core.config import settings

# GitHub API base
GITHUB_API = "https://api.github.com"


def _generate_jwt() -> str:
    """
    Generate a JSON Web Token (JWT) for GitHub App authentication.
    JWTs are valid for 10 minutes max.
    """
    now = int(time.time())
    payload = {
        "iat": now - 60,              # issued at (60s in the past for clock drift)
        "exp": now + (9 * 60),        # expires in 9 minutes
        "iss": settings.github_app_id,
    }
    return jwt.encode(
        payload,
        settings.github_app_private_key,
        algorithm="RS256",
    )


async def get_installation_token(installation_id: int) -> str:
    """
    Exchange the App JWT for an installation access token.
    These tokens are scoped to a specific installation (repo/org)
    and last for 1 hour.
    """
    app_jwt = _generate_jwt()

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["token"]


class GitHubClient:
    """
    Authenticated GitHub API client for a specific installation.
    All GitHub tools use this to make API calls.
    """

    def __init__(self, installation_id: int):
        self.installation_id = installation_id
        self._token: Optional[str] = None
        self._token_expires_at: float = 0

    async def _ensure_token(self) -> str:
        """Get a valid token, refreshing if expired."""
        if not self._token or time.time() > self._token_expires_at - 300:
            self._token = await get_installation_token(self.installation_id)
            self._token_expires_at = time.time() + 3600  # 1 hour
        return self._token

    async def request(
        self,
        method: str,
        endpoint: str,
        **kwargs,
    ) -> httpx.Response:
        """Make an authenticated request to the GitHub API."""
        token = await self._ensure_token()
        async with httpx.AsyncClient() as client:
            response = await client.request(
                method,
                f"{GITHUB_API}{endpoint}",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                **kwargs,
            )
            response.raise_for_status()
            return response

    async def get(self, endpoint: str, **kwargs) -> dict:
        """GET request, returns JSON."""
        response = await self.request("GET", endpoint, **kwargs)
        return response.json()

    async def post(self, endpoint: str, **kwargs) -> dict:
        """POST request, returns JSON."""
        response = await self.request("POST", endpoint, **kwargs)
        return response.json()

    async def patch(self, endpoint: str, **kwargs) -> dict:
        """PATCH request, returns JSON."""
        response = await self.request("PATCH", endpoint, **kwargs)
        return response.json()
