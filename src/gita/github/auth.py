"""GitHub App authentication — pure JWT generation, zero I/O.

Keeping this module I/O-free makes it trivially unit-testable (decode the
JWT, verify structure) without needing a mock HTTP transport. The
``GithubClient`` in ``client.py`` handles all the HTTP parts — installation
lookup, token exchange, API calls.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import jwt

# GitHub's hard cap on JWT expiration is 10 minutes. We use slightly less
# to account for clock drift across the wire.
_JWT_TTL_SECONDS = 9 * 60
# Back-date iat by 60 seconds so a slightly-fast local clock doesn't
# produce a JWT that GitHub considers "issued in the future."
_JWT_IAT_SKEW = 60


@dataclass
class GithubAppAuth:
    """Immutable app credentials. Load once at startup, reuse for every
    JWT you need to sign."""

    app_id: int
    private_key: bytes  # PEM-encoded RSA private key

    @classmethod
    def from_files(
        cls, app_id: int, private_key_path: str | Path
    ) -> "GithubAppAuth":
        path = Path(private_key_path)
        if not path.is_file():
            raise FileNotFoundError(
                f"GitHub App private key not found at {path}"
            )
        return cls(app_id=app_id, private_key=path.read_bytes())

    def generate_jwt(self, *, now: float | None = None) -> str:
        """Sign a JWT for talking to ``/app/*`` endpoints.

        Payload: ``{iat, exp, iss}`` per GitHub's docs. Signed with RS256.
        The optional ``now`` parameter lets tests pin a clock value.
        """
        current = time.time() if now is None else now
        payload = {
            "iat": int(current) - _JWT_IAT_SKEW,
            "exp": int(current) + _JWT_TTL_SECONDS,
            # PyJWT 2.10+ requires iss to be a string.
            "iss": str(self.app_id),
        }
        token = jwt.encode(payload, self.private_key, algorithm="RS256")
        # PyJWT 2.x returns str; older returned bytes. Normalize to str.
        if isinstance(token, bytes):
            return token.decode("utf-8")
        return token
