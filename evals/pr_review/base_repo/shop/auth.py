"""Passwords, sessions and permission checks."""

import hashlib
import hmac
import secrets
from dataclasses import dataclass

_ITERATIONS = 200_000


@dataclass(frozen=True)
class User:
    id: int
    email: str
    is_admin: bool
    is_active: bool


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt_hex, digest_hex = stored.split("$", 1)
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), _ITERATIONS
    )
    return hmac.compare_digest(candidate.hex(), digest_hex)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def session_matches(presented: str, expected: str) -> bool:
    return hmac.compare_digest(presented, expected)


def require_active(user: User | None) -> User:
    if user is None or not user.is_active:
        raise PermissionError("login required")
    return user


def require_admin(user: User | None) -> User:
    user = require_active(user)
    if not user.is_admin:
        raise PermissionError("admin only")
    return user
