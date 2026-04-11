"""Fixtures for the GitHub client tests.

Generates a throwaway RSA keypair once per session so tests can sign and
verify real JWTs without touching the filesystem. ~1 second cost amortized
across every test in ``tests/github/``.
"""
from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from gita.github.auth import GithubAppAuth

TEST_APP_ID = 123456


@pytest.fixture(scope="session")
def rsa_keypair() -> tuple[bytes, bytes]:
    """Return (private_pem, public_pem) for a fresh 2048-bit RSA keypair."""
    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


@pytest.fixture(scope="session")
def test_auth(rsa_keypair: tuple[bytes, bytes]) -> GithubAppAuth:
    private_pem, _ = rsa_keypair
    return GithubAppAuth(app_id=TEST_APP_ID, private_key=private_pem)
