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


def parse_name_status(output: str) -> list[FileChange]:
    """Parse the output of ``git diff --name-status``.

    Each line is ``<status>[score]\\t<path>`` or for renames/copies
    ``<status>[score]\\t<old_path>\\t<new_path>``.

    Returns a list of ``FileChange`` in the order they appear.
    Unknown status codes are logged and skipped.
    """
    changes: list[FileChange] = []
    for line in output.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            logger.warning("diff_parse_skip line=%r", line)
            continue

        raw_status = parts[0].strip()
        # Status may have a score suffix for renames: "R100", "R085"
        status_letter = raw_status[0] if raw_status else ""
        status = _STATUS_MAP.get(status_letter)

        if status is None:
            logger.warning(
                "diff_unknown_status status=%r line=%r", raw_status, line
            )
            continue

        if status == "renamed" and len(parts) >= 3:
            old_path = parts[1].strip().replace("\\", "/")
            new_path = parts[2].strip().replace("\\", "/")
            # A rename produces two changes: delete the old, add the new.
            changes.append(
                FileChange(
                    relative_path=old_path,
                    status="deleted",
                    previous_path=None,
                )
            )
            changes.append(
                FileChange(
                    relative_path=new_path,
                    status="added",
                    previous_path=old_path,
                )
            )
        elif status_letter == "C" and len(parts) >= 3:
            # Copy: the destination is a new file; source is unchanged.
            new_path = parts[2].strip().replace("\\", "/")
            changes.append(
                FileChange(relative_path=new_path, status="added")
            )
        else:
            file_path = parts[1].strip().replace("\\", "/")
            changes.append(
                FileChange(relative_path=file_path, status=status)
            )

    return changes


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
                "diff", "--name-status", old_sha, "HEAD",
            ],
            capture_output=True,
            text=True,
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

    return parse_name_status(result.stdout)


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
