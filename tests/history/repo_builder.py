"""Build git repositories with a known history, with real git commands."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


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
