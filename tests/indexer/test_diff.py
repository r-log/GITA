"""Tests for the git diff detection module.

``parse_name_status`` is pure (no I/O) — tested with fixture strings.
``detect_changes`` shells out to git — tested against a real tmpdir
git repo with controlled commits.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gita.indexer.diff import (
    FileChange,
    detect_changes,
    parse_name_status,
    discover_default_branch,
    read_head_sha,
)


# ---------------------------------------------------------------------------
# Pure parser: parse_name_status
# ---------------------------------------------------------------------------
class TestParseNameStatus:
    def test_modified_file(self):
        output = "M\tsrc/app.py\n"
        changes = parse_name_status(output)
        assert changes == [FileChange("src/app.py", "modified")]

    def test_added_file(self):
        output = "A\tsrc/new.py\n"
        changes = parse_name_status(output)
        assert changes == [FileChange("src/new.py", "added")]

    def test_deleted_file(self):
        output = "D\tsrc/old.py\n"
        changes = parse_name_status(output)
        assert changes == [FileChange("src/old.py", "deleted")]

    def test_renamed_file_splits_into_delete_and_add(self):
        """A rename produces two FileChanges: delete old + add new."""
        output = "R100\tsrc/old.py\tsrc/new.py\n"
        changes = parse_name_status(output)
        assert len(changes) == 2
        assert changes[0] == FileChange("src/old.py", "deleted")
        assert changes[1] == FileChange(
            "src/new.py", "added", previous_path="src/old.py"
        )

    def test_type_change_treated_as_modified(self):
        output = "T\tsrc/script.py\n"
        changes = parse_name_status(output)
        assert changes == [FileChange("src/script.py", "modified")]

    def test_copied_treated_as_added(self):
        output = "C100\tsrc/orig.py\tsrc/copy.py\n"
        changes = parse_name_status(output)
        assert changes == [FileChange("src/copy.py", "added")]

    def test_multiple_files(self):
        output = "M\tsrc/a.py\nA\tsrc/b.py\nD\tsrc/c.py\n"
        changes = parse_name_status(output)
        assert len(changes) == 3
        assert changes[0].status == "modified"
        assert changes[1].status == "added"
        assert changes[2].status == "deleted"

    def test_empty_output(self):
        assert parse_name_status("") == []
        assert parse_name_status("\n") == []

    def test_backslash_normalized(self):
        output = "M\tsrc\\app.py\n"
        changes = parse_name_status(output)
        assert changes[0].relative_path == "src/app.py"

    def test_unknown_status_skipped(self):
        output = "X\tsrc/mystery.py\nM\tsrc/known.py\n"
        changes = parse_name_status(output)
        assert len(changes) == 1
        assert changes[0].relative_path == "src/known.py"

    def test_rename_with_partial_score(self):
        output = "R085\tsrc/old_name.py\tsrc/new_name.py\n"
        changes = parse_name_status(output)
        assert len(changes) == 2
        assert changes[0].status == "deleted"
        assert changes[1].status == "added"
        assert changes[1].previous_path == "src/old_name.py"


# ---------------------------------------------------------------------------
# Integration: detect_changes + read_head_sha against a real git repo
# ---------------------------------------------------------------------------
@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with two commits."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", str(repo)], capture_output=True, check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        capture_output=True, check=True,
    )

    # Commit 1: initial file
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "."],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        capture_output=True, check=True,
    )

    # Commit 2: modify a.py, add b.py, delete nothing
    (repo / "a.py").write_text("x = 2\n")
    (repo / "b.py").write_text("y = 1\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "."],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "second"],
        capture_output=True, check=True,
    )

    return repo


def _get_sha(repo: Path, ref: str = "HEAD") -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


class TestReadHeadSha:
    def test_reads_current_head(self, git_repo: Path):
        sha = read_head_sha(git_repo)
        assert sha is not None
        assert len(sha) == 40

    def test_non_git_dir_returns_none(self, tmp_path: Path):
        assert read_head_sha(tmp_path) is None


class TestDiscoverDefaultBranch:
    def test_repo_without_remote_falls_back_to_main(
        self, git_repo: Path
    ):
        """Local-only repos (no origin) → fallback ``"main"``.

        Both tiers (symbolic-ref + ``git remote show origin``) come up
        empty without an origin, so we land on the hard fallback.
        """
        assert discover_default_branch(git_repo) == "main"

    def test_non_git_dir_falls_back_to_main(self, tmp_path: Path):
        """Even outside a git checkout the helper must return *something*
        — index_repository's call site should never blow up on it."""
        assert discover_default_branch(tmp_path) == "main"

    def test_symbolic_ref_path(self, git_repo: Path):
        """Tier 1 wins when ``refs/remotes/origin/HEAD`` is set.

        We fake an origin remote (URL doesn't need to be reachable) and
        write the symbolic ref directly so we don't need a network round
        trip in the test.
        """
        subprocess.run(
            [
                "git", "-C", str(git_repo), "remote", "add", "origin",
                "https://example.invalid/x.git",
            ],
            capture_output=True, check=True,
        )
        # Use an unusual branch name so a regression to "main" stands
        # out clearly in the assertion failure.
        subprocess.run(
            [
                "git", "-C", str(git_repo),
                "symbolic-ref", "refs/remotes/origin/HEAD",
                "refs/remotes/origin/trunk",
            ],
            capture_output=True, check=True,
        )
        assert discover_default_branch(git_repo) == "trunk"


class TestDetectChanges:
    def test_detects_modified_and_added(self, git_repo: Path):
        first_sha = _get_sha(git_repo, "HEAD~1")
        changes = detect_changes(git_repo, first_sha)
        assert changes is not None

        paths = {c.relative_path: c.status for c in changes}
        assert paths.get("a.py") == "modified"
        assert paths.get("b.py") == "added"

    def test_no_changes_returns_empty(self, git_repo: Path):
        head_sha = _get_sha(git_repo)
        changes = detect_changes(git_repo, head_sha)
        assert changes is not None
        assert changes == []

    def test_invalid_sha_returns_none(self, git_repo: Path):
        changes = detect_changes(git_repo, "0000000000000000000000000000000000000000")
        assert changes is None

    def test_non_git_dir_returns_none(self, tmp_path: Path):
        changes = detect_changes(tmp_path, "abc123")
        assert changes is None

    def test_detects_deletion(self, git_repo: Path):
        """Add a third commit that deletes b.py."""
        first_sha = _get_sha(git_repo)
        (git_repo / "b.py").unlink()
        subprocess.run(
            ["git", "-C", str(git_repo), "add", "-A"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-m", "delete b"],
            capture_output=True, check=True,
        )
        changes = detect_changes(git_repo, first_sha)
        assert changes is not None
        paths = {c.relative_path: c.status for c in changes}
        assert paths.get("b.py") == "deleted"
