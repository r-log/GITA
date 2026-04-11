import os

from .models import User
from .utils import format_name


def create_user(name: str, email: str) -> User:
    return User(name=format_name(name), email=email)


def main() -> None:
    user = create_user("alice", "a@b.co")
    print(user.display_name())
    print("HOME=", os.environ.get("HOME", ""))
