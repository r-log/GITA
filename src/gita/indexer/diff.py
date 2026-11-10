"""Git diff detection for incremental re-indexing.

Given a repo root and the SHA of the last successful index, detects which
files changed (added, modified, deleted, renamed) so the ingest pipeline
can re-parse only the affected files instead of nuking and rebuilding.

The detection shells out to ``git diff --name-status`` — same approach as
``_read_head_sha`` in ``ingest.py``. The parser is pure (no I/O) so it
can be tested without a real git repo.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileChange:
    """One file's contribution to a diff between two commits."""

    relative_path: str  # forward-slash normalized
    status: str  # "added" | "modified" | "deleted" | "renamed"
    previous_path: str | None = None  # set for renames


# Map git's single-letter status codes to our status names.
_STATUS_MAP: dict[str, str] = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "T": "modified",  # type change → treat as modified
    "C": "added",     # copied → treat as added
}


def _records_to_changes(records: list[list[str]]) -> list[FileChange]:
    """Turn ``[status, path, (new_path)]`` records into FileChanges."""
    changes: list[FileChange] = []
    for record in records:
        raw_status = record[0].strip() if record else ""
        status_letter = raw_status[:1]
        status = _STATUS_MAP.get(status_letter)
        if status is None or len(record) < 2:
            logger.warning("diff_unknown_status record=%r", record)
            continue
        paths = [p.replace("\\", "/") for p in record[1:]]
        if status == "renamed" and len(paths) >= 2:
            # A rename is a delete of the old path plus an add of the new.
            changes.append(FileChange(relative_path=paths[0], status="deleted"))
            changes.append(
                FileChange(relative_path=paths[1], status="added", previous_path=paths[0])
            )
        elif status_letter == "C" and len(paths) >= 2:
            # Copy: the destination is new; the source is unchanged.
            changes.append(FileChange(relative_path=paths[1], status="added"))
        else:
            changes.append(FileChange(relative_path=paths[0], status=status))
    return changes


def parse_name_status(output: str) -> list[FileChange]:
    """Parse human-readable ``git diff --name-status`` output.

    Each line is ``<status>[score]\\t<path>`` or, for renames/copies,
    ``<status>[score]\\t<old_path>\\t<new_path>``. Paths with unusual
    characters arrive C-quoted in this format; ``detect_changes`` uses
    ``-z`` output (``parse_name_status_z``) for that reason.
    """
    records = [
        [part.strip() for part in line.split("\t")]
        for line in output.strip().splitlines()
        if line.strip()
    ]
    return _records_to_changes(records)


def parse_name_status_z(output: str) -> list[FileChange]:
    """Parse ``git diff --name-status -z`` output.

    Fields are NUL-terminated and paths are verbatim (no quoting), so
    non-ASCII and otherwise unusual file names survive. Renames and
    copies carry two paths after the status.
    """
    tokens = output.split("\0")
    records: list[list[str]] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        if not status:
            i += 1
            continue
        width = 2 if status[:1] in ("R", "C") else 1
        records.append([status, *tokens[i + 1 : i + 1 + width]])
        i += 1 + width
    return _records_to_changes(records)


def detect_changes(
    root: Path, old_sha: str
) -> list[FileChange] | None:
    """Detect changed files between ``old_sha`` and HEAD.

    Returns:
    - ``list[FileChange]`` — the changes (may be empty if nothing changed)
    - ``None`` — if git is unavailable, the old_sha is invalid, or the
      diff command fails. The caller should fall back to a full re-index.
    """
    try:
        result = subprocess.run(
            [
                "git", "-C", str(root),
                "diff", "--name-status", "-z", old_sha, "HEAD",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("diff_detect_failed root=%s error=%s", root, exc)
        return None

    if result.returncode != 0:
        logger.warning(
            "diff_detect_nonzero root=%s old_sha=%s stderr=%s",
            root,
            old_sha,
            result.stderr.strip()[:200],
        )
        return None

    return parse_name_status_z(result.stdout)


def tracked_files(root: Path) -> list[str] | None:
    """Paths tracked by git at HEAD's index (``git ls-files -z``).

    ``None`` when ``root`` isn't a git work tree; callers then walk the
    filesystem instead.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return [p for p in result.stdout.split("\0") if p]


def read_head_sha(root: Path) -> str | None:
    """Read the current HEAD SHA. Extracted from ingest.py for reuse."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


# Hard fallback when both discovery tiers come up empty. "main" is GitHub's
# default for new repos since 2020 and the closest thing to a safe guess.
_DEFAULT_BRANCH_FALLBACK = "main"


def discover_default_branch(root: Path) -> str:
    """Resolve the remote's default branch name, with two-tier fallback.

    Strategy (first non-empty wins):

    1. ``git symbolic-ref --short refs/remotes/origin/HEAD``.
       Cheap, offline, but only set if someone ran
       ``git remote set-head origin -a`` (or cloned fresh — clone sets it).
    2. ``git remote show origin`` — parse ``HEAD branch: …``.
       Reliable but issues a network round-trip on most setups.
    3. Hard fallback to ``"main"``.

    Returns the bare branch name (e.g. ``"main"``) — never the
    ``"origin/main"`` form, since callers feed it straight into things
    like ``f"refs/heads/{branch}"``.
    """
    # ---- Tier 1: symbolic-ref (cheap, offline) ----
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "symbolic-ref",
                "--short",
                "refs/remotes/origin/HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            value = result.stdout.strip()
            if value:
                # symbolic-ref returns e.g. "origin/main" — strip the prefix.
                return value.split("/", 1)[1] if "/" in value else value
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # ---- Tier 2: parse `git remote show origin` (may hit network) ----
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "remote", "show", "origin"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                stripped = line.strip()
                # Format is exactly "HEAD branch: <name>" — locale-stable
                # since git's plumbing-ish porcelain output for this
                # command is English-only.
                if stripped.startswith("HEAD branch:"):
                    candidate = stripped.split(":", 1)[1].strip()
                    if candidate and candidate != "(unknown)":
                        return candidate
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # ---- Tier 3: hard fallback ----
    logger.info(
        "default_branch_fallback root=%s using=%s",
        root,
        _DEFAULT_BRANCH_FALLBACK,
    )
    return _DEFAULT_BRANCH_FALLBACK
