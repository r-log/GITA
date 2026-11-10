"""A small repository with a known history, built with real git commands."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def _path(key: str) -> str:
    """Keyword names stand for paths: "src__shop__cart_py" -> "src/shop/cart.py"."""
    head, _, ext = key.rpartition("_")
    return head.replace("__", "/") + "." + ext


class RepoBuilder:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._tick = 0
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        self._tick += 1
        stamp = f"2026-01-01T00:{self._tick // 60:02d}:{self._tick % 60:02d}+00:00"
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Dev",
            "GIT_AUTHOR_EMAIL": "dev@example.invalid",
            "GIT_COMMITTER_NAME": "Dev",
            "GIT_COMMITTER_EMAIL": "dev@example.invalid",
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_DATE": stamp,
        }
        return subprocess.run(
            ["git", "-C", str(self.root), "-c", "commit.gpgsign=false", *args],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        ).stdout

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, message: str, **files: str) -> str:
        for rel, text in files.items():
            self.write(_path(rel), text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def pr(self, number: int, branch: str, title: str, **files: str) -> str:
        """Open a branch, commit, and merge it with a GitHub-style merge commit."""
        self.git("checkout", "-q", "-b", branch)
        self.commit(title, **files)
        self.git("checkout", "-q", "main")
        self.git(
            "merge",
            "-q",
            "--no-ff",
            branch,
            "-m",
            f"Merge pull request #{number} from acme/{branch}",
            "-m",
            title,
        )
        return self.git("rev-parse", "HEAD").strip()


CART_V0 = "def total(prices):\n    return sum(prices)\n"
CART_V1 = CART_V0 + "\n\ndef discounted(price, percent):\n    return price * percent / 100\n"
CART_V2 = CART_V1.replace("price * percent / 100", "price * (100 - percent) / 100")


@pytest.fixture
def shop_repo(tmp_path: Path) -> RepoBuilder:
    """main: an inherited upstream squash commit "(#1)", then

    #1 feat/discount   adds discounted() with a bug (line 6)
    #2 docs/readme     README only
    #3 fix/discount    fixes line 6                      -> introduced by #1
    #4 (squash) fix:   rewrites total()'s return line   -> code older than any PR
    #5 fix/test-typo   touches only tests/              -> nothing to trace
    """
    (tmp_path / "shop").mkdir()
    r = RepoBuilder(tmp_path / "shop")
    r.commit(
        "Upstream: initial cart (#1)", src__shop__cart_py=CART_V0, tests__test_cart_py="x = 1\n"
    )
    r.pr(1, "feat/discount", "Add discounts", src__shop__cart_py=CART_V1)
    r.pr(2, "docs/readme", "Document the cart", README_md="# Shop\n")
    r.pr(3, "fix/discount", "Discount subtracts the percentage", src__shop__cart_py=CART_V2)
    r.commit(
        "fix: empty cart totals 0 (#4)",
        src__shop__cart_py=CART_V2.replace(
            "return sum(prices)", "return sum(prices) if prices else 0"
        ),
    )
    r.pr(5, "fix/test-typo", "Fix a test typo", tests__test_cart_py="x = 2\n")
    return r
