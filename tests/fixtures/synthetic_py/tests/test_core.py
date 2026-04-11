"""This file must be SKIPPED by the walker (tests/ dir excluded by default)."""
from myapp.core import create_user


def test_create_user_returns_user():
    user = create_user("bob", "b@c.de")
    assert user.name == "Bob"
