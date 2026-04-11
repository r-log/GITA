"""Tests for GithubAppAuth JWT generation.

No HTTP, no GitHub. Just sign JWTs with a throwaway RSA key and verify that
the result decodes correctly against the matching public key.
"""
from __future__ import annotations

import time

import jwt
import pytest

from gita.github.auth import GithubAppAuth

from tests.github.conftest import TEST_APP_ID


class TestJwtGeneration:
    def test_generates_a_valid_jwt_string(self, test_auth):
        token = test_auth.generate_jwt()
        assert isinstance(token, str)
        assert token.count(".") == 2  # header.payload.signature

    def test_payload_has_required_fields(self, test_auth):
        token = test_auth.generate_jwt()
        decoded = jwt.decode(
            token, options={"verify_signature": False}
        )
        assert set(decoded.keys()) == {"iat", "exp", "iss"}
        # PyJWT 2.10+ requires iss to be a string.
        assert decoded["iss"] == str(TEST_APP_ID)

    def test_exp_is_at_most_ten_minutes_from_now(self, test_auth):
        token = test_auth.generate_jwt()
        decoded = jwt.decode(
            token, options={"verify_signature": False}
        )
        ttl = decoded["exp"] - time.time()
        assert ttl > 0
        assert ttl <= 10 * 60 + 1  # 10 min cap + a tiny jitter

    def test_iat_back_dated_to_allow_clock_skew(self, test_auth):
        token = test_auth.generate_jwt()
        decoded = jwt.decode(
            token, options={"verify_signature": False}
        )
        assert decoded["iat"] < time.time()

    def test_now_parameter_pins_the_clock(self, test_auth):
        fixed_now = 1_700_000_000.0
        token = test_auth.generate_jwt(now=fixed_now)
        decoded = jwt.decode(
            token, options={"verify_signature": False}
        )
        assert decoded["iss"] == str(TEST_APP_ID)
        # iat is back-dated by 60 seconds, exp is forward ~9 minutes
        assert decoded["iat"] == int(fixed_now) - 60
        assert decoded["exp"] == int(fixed_now) + 9 * 60

    def test_signature_verifies_against_public_key(
        self, test_auth, rsa_keypair
    ):
        _, public_pem = rsa_keypair
        token = test_auth.generate_jwt()
        decoded = jwt.decode(
            token,
            public_pem,
            algorithms=["RS256"],
            options={"verify_signature": True},
        )
        assert decoded["iss"] == str(TEST_APP_ID)

    def test_wrong_public_key_rejects_signature(self, test_auth):
        """A JWT signed with key A cannot be verified with key B."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        wrong_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        wrong_public = wrong_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        token = test_auth.generate_jwt()
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(token, wrong_public, algorithms=["RS256"])


class TestFromFiles:
    def test_from_files_loads_pem(self, tmp_path, rsa_keypair):
        private_pem, _ = rsa_keypair
        pem_path = tmp_path / "app.pem"
        pem_path.write_bytes(private_pem)

        auth = GithubAppAuth.from_files(app_id=42, private_key_path=pem_path)
        assert auth.app_id == 42
        assert auth.private_key == private_pem

        # The loaded auth should be able to sign a valid JWT
        token = auth.generate_jwt()
        decoded = jwt.decode(token, options={"verify_signature": False})
        assert decoded["iss"] == "42"

    def test_missing_pem_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="private key"):
            GithubAppAuth.from_files(
                app_id=42, private_key_path=tmp_path / "nope.pem"
            )
