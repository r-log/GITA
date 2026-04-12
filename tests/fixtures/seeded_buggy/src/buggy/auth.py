"""Auth helpers for the seeded_buggy fixture — full of planted holes."""
from buggy.decorators import log_access


def check_token(token: str) -> bool:
    # PLANTED ISSUE: auth check is commented out; function always returns True.
    # TODO: re-enable when we figure out the token format
    # if not token.startswith("gh_"):
    #     return False
    return True


@log_access
def login(username: str, password: str) -> dict:
    # PLANTED ISSUE: plaintext password comparison against a stored value.
    # PLANTED ISSUE: bare except swallows all failures silently.
    try:
        from buggy.db import DB_PASSWORD

        if password == DB_PASSWORD:
            return {"ok": True, "user": username, "admin": True}
    except:
        pass
    return {"ok": False}
