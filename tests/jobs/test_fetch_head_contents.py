"""fetch_head_contents: which changed files get head-side context."""

from __future__ import annotations

import httpx

from gita.agents.pr_reviewer.diff_parser import DiffHunk
from gita.github.client import FileContents
from gita.jobs.runners import fetch_head_contents


def _hunk(path: str, status: str = "modified", patch: str | None = "@@") -> DiffHunk:
    return DiffHunk(file_path=path, status=status, additions=1, deletions=0, patch=patch)


class _FakeGithub:
    def __init__(self, files: dict[str, FileContents | Exception]):
        self.files = files
        self.refs: list[str | None] = []

    async def get_contents(self, owner, repo, path, *, ref=None):
        self.refs.append(ref)
        value = self.files[path]
        if isinstance(value, Exception):
            raise value
        return value


def _fc(path: str, content: str, *, encoding: str = "base64", size: int | None = None):
    return FileContents(
        path=path,
        content=content,
        sha="s",
        encoding=encoding,
        size=len(content) if size is None else size,
    )


async def test_fetches_text_files_at_the_head_commit():
    gh = _FakeGithub(
        {
            "a.py": _fc("a.py", "x = 1\n"),
            "gone.py": _fc("gone.py", "unused"),
            "logo.png": _fc("logo.png", ""),
            "huge.json": _fc("huge.json", "", encoding="none", size=5_000_000),
            "missing.py": httpx.HTTPStatusError(
                "404", request=httpx.Request("GET", "x"), response=httpx.Response(404)
            ),
            "latin1.txt": UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
        }
    )
    hunks = [
        _hunk("a.py"),
        _hunk("gone.py", status="removed"),
        _hunk("logo.png", patch=None),  # binary: GitHub sends no patch
        _hunk("huge.json"),
        _hunk("missing.py"),
        _hunk("latin1.txt"),
    ]

    result = await fetch_head_contents(gh, "o", "r", hunks, "f" * 40)

    assert result == {"a.py": "x = 1\n"}
    assert set(gh.refs) == {"f" * 40}


async def test_no_head_sha_fetches_nothing():
    gh = _FakeGithub({})
    assert await fetch_head_contents(gh, "o", "r", [_hunk("a.py")], None) == {}
    assert gh.refs == []
