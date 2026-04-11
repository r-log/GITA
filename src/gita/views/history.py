"""``history_view`` — recent commits + blame summary for a file.

Shells out to local git. The repo must have a working tree at
``Repo.root_path``; if git isn't available or the path isn't a git repo, we
return an empty result instead of raising — agents can still call the view
without having to pre-check.
"""
from __future__ import annotations

import asyncio
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from gita.views._common import resolve_repo

MAX_COMMITS = 10
GIT_TIMEOUT = 10  # seconds

_LOG_FORMAT = "%H%x1f%h%x1f%an%x1f%aI%x1f%s%x1e"


@dataclass
class CommitInfo:
    sha: str
    short_sha: str
    author: str
    date: str  # ISO-8601 from git's %aI
    message: str


@dataclass
class HistoryResult:
    file_path: str
    recent_commits: list[CommitInfo] = field(default_factory=list)
    blame_summary: dict[str, int] = field(default_factory=dict)
    git_available: bool = True


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _parse_log(stdout: str) -> list[CommitInfo]:
    commits: list[CommitInfo] = []
    # _LOG_FORMAT uses 0x1f as field separator and 0x1e as record separator
    for record in stdout.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        fields = record.split("\x1f")
        if len(fields) != 5:
            continue
        sha, short_sha, author, date, message = fields
        commits.append(
            CommitInfo(
                sha=sha,
                short_sha=short_sha,
                author=author,
                date=date,
                message=message,
            )
        )
    return commits


def _parse_blame(stdout: str) -> dict[str, int]:
    """git blame --line-porcelain — count lines per author."""
    counts: Counter[str] = Counter()
    for line in stdout.splitlines():
        if line.startswith("author "):
            counts[line[len("author ") :]] += 1
    return dict(counts)


def _history_sync(root: Path, file_path: str) -> HistoryResult:
    result = HistoryResult(file_path=file_path)

    log_proc = _run_git(
        [
            "log",
            f"--pretty=format:{_LOG_FORMAT}",
            f"-{MAX_COMMITS}",
            "--follow",
            "--",
            file_path,
        ],
        cwd=root,
    )
    if log_proc is None:
        result.git_available = False
        return result
    if log_proc.returncode == 0 and log_proc.stdout:
        result.recent_commits = _parse_log(log_proc.stdout)

    blame_proc = _run_git(
        ["blame", "--line-porcelain", "--", file_path], cwd=root
    )
    if blame_proc is not None and blame_proc.returncode == 0 and blame_proc.stdout:
        result.blame_summary = _parse_blame(blame_proc.stdout)

    return result


async def history_view(
    session: AsyncSession, repo_name: str, file_path: str
) -> HistoryResult:
    """Return git log + blame summary for ``file_path`` in the named repo."""
    repo = await resolve_repo(session, repo_name)
    file_path = file_path.replace("\\", "/")
    root = Path(repo.root_path)

    if not root.is_dir():
        return HistoryResult(file_path=file_path, git_available=False)

    # Run blocking git shell-outs in a thread to not starve the event loop
    return await asyncio.to_thread(_history_sync, root, file_path)
