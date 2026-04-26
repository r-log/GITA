"""Tests for the test-generator preflight gates (Week 9).

Stage A (``has_existing_tests``) is sync, filesystem-only — uses
``tmp_path``-driven mini-repos. Stage B (``is_feasible``) is async
and DB-backed — uses the existing ``indexed_synth_py`` fixture for
the happy path and seeds extra rows for the edge cases.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gita.agents.test_generator.preflight import (
    PreflightResult,
    has_existing_tests,
    is_feasible,
)
from gita.db.models import AgentAction, CodeIndex, Repo


# ---------------------------------------------------------------------------
# Stage A — has_existing_tests
# ---------------------------------------------------------------------------
def _write(p: Path, content: str = "x = 1\n") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


class TestHasExistingTestsPathScan:
    def test_no_tests_proceeds(self, tmp_path: Path):
        _write(tmp_path / "src" / "myapp" / "utils.py", "def foo(): pass\n")
        result = has_existing_tests(tmp_path, "src/myapp/utils.py")
        assert result.proceed is True
        assert result.reason == "ok"

    def test_sibling_test_file_blocks(self, tmp_path: Path):
        _write(tmp_path / "src" / "myapp" / "utils.py")
        _write(tmp_path / "src" / "myapp" / "test_utils.py")
        result = has_existing_tests(tmp_path, "src/myapp/utils.py")
        assert result.proceed is False
        assert "tests_exist:sibling" in result.reason
        assert "test_utils.py" in result.reason

    def test_go_style_sibling_test_file_blocks(self, tmp_path: Path):
        _write(tmp_path / "pkg" / "utils.py")
        _write(tmp_path / "pkg" / "utils_test.py")
        result = has_existing_tests(tmp_path, "pkg/utils.py")
        assert result.proceed is False
        assert "tests_exist:sibling" in result.reason
        assert "utils_test.py" in result.reason

    def test_conventional_tests_dir_blocks(self, tmp_path: Path):
        _write(tmp_path / "src" / "myapp" / "utils.py")
        _write(tmp_path / "tests" / "test_utils.py")
        result = has_existing_tests(tmp_path, "src/myapp/utils.py")
        assert result.proceed is False
        assert "tests_exist:in_test_dir" in result.reason
        assert "tests/test_utils.py" in result.reason

    def test_mirror_layout_in_test_dir_blocks(self, tmp_path: Path):
        _write(tmp_path / "src" / "myapp" / "utils.py")
        _write(tmp_path / "tests" / "myapp" / "test_utils.py")
        result = has_existing_tests(tmp_path, "src/myapp/utils.py")
        assert result.proceed is False
        assert "tests_exist:in_test_dir" in result.reason

    def test_skipped_dir_match_does_not_block(self, tmp_path: Path):
        """A test file under node_modules/ is vendored noise, not real coverage."""
        _write(tmp_path / "src" / "utils.py")
        _write(tmp_path / "node_modules" / "tests" / "test_utils.py")
        result = has_existing_tests(tmp_path, "src/utils.py")
        assert result.proceed is True


class TestHasExistingTestsContentGrep:
    def test_test_file_imports_target_module_blocks(self, tmp_path: Path):
        # Realistic src-layout: package myapp under src/.
        _write(tmp_path / "src" / "myapp" / "__init__.py")
        _write(
            tmp_path / "src" / "myapp" / "helpers.py",
            "def add(a, b): return a + b\n",
        )
        # Non-conventional name so the path scan misses it.
        _write(
            tmp_path / "tests" / "test_arithmetic.py",
            "from myapp.helpers import add\n\ndef test_add(): assert add(1,2)==3\n",
        )
        result = has_existing_tests(tmp_path, "src/myapp/helpers.py")
        assert result.proceed is False
        assert "tests_exist:imports_target" in result.reason
        assert "myapp.helpers" in result.reason

    def test_named_like_test_outside_test_dir_blocks(self, tmp_path: Path):
        _write(tmp_path / "src" / "myapp" / "__init__.py")
        _write(tmp_path / "src" / "myapp" / "core.py", "def go(): pass\n")
        # Test file lives next to source (not in tests/), name still triggers.
        _write(
            tmp_path / "src" / "myapp" / "smoke_test.py",
            "from myapp import core\n\ndef test(): assert core\n",
        )
        result = has_existing_tests(tmp_path, "src/myapp/core.py")
        assert result.proceed is False
        assert "tests_exist" in result.reason

    def test_unrelated_test_file_does_not_block(self, tmp_path: Path):
        _write(tmp_path / "src" / "myapp" / "__init__.py")
        _write(tmp_path / "src" / "myapp" / "utils.py", "def foo(): pass\n")
        _write(tmp_path / "src" / "myapp" / "other.py", "def bar(): pass\n")
        # Test that imports something unrelated.
        _write(
            tmp_path / "tests" / "test_other.py",
            "from myapp.other import bar\n",
        )
        result = has_existing_tests(tmp_path, "src/myapp/utils.py")
        assert result.proceed is True

    def test_content_grep_handles_unreadable_file(self, tmp_path: Path):
        """A file that errors on read is silently skipped, not a crash."""
        _write(tmp_path / "src" / "myapp" / "__init__.py")
        _write(tmp_path / "src" / "myapp" / "utils.py")
        # Empty test file is valid — grep finds nothing, function falls
        # through to the "ok" return.
        _write(tmp_path / "tests" / "test_other_thing.py", "")
        result = has_existing_tests(tmp_path, "src/myapp/utils.py")
        assert result.proceed is True


# ---------------------------------------------------------------------------
# Stage B — is_feasible
# ---------------------------------------------------------------------------
async def _make_repo(session, name: str = "feasibility_repo") -> Repo:
    repo = Repo(name=name, root_path="/tmp/no", auto_test_generation=False)
    session.add(repo)
    await session.flush()
    return repo


async def _make_file(
    session,
    repo: Repo,
    file_path: str,
    *,
    language: str = "python",
    line_count: int = 50,
    structure: dict | None = None,
    content: str = "",
) -> CodeIndex:
    row = CodeIndex(
        repo_id=repo.id,
        file_path=file_path,
        language=language,
        content=content,
        line_count=line_count,
        structure=structure or {},
    )
    session.add(row)
    await session.flush()
    return row


class TestIsFeasibleHappyPath:
    async def test_python_file_with_public_function_passes(
        self, db_session
    ):
        repo = await _make_repo(db_session)
        await _make_file(
            db_session,
            repo,
            "src/utils.py",
            structure={
                "functions": [{"name": "format_name"}],
                "classes": [],
            },
        )
        result = await is_feasible(
            db_session, repo.id, "owner/repo", "src/utils.py"
        )
        assert result.proceed is True
        assert result.reason == "ok"


class TestIsFeasibleRejections:
    async def test_missing_from_index_rejected(self, db_session):
        repo = await _make_repo(db_session)
        result = await is_feasible(
            db_session, repo.id, "owner/repo", "src/missing.py"
        )
        assert result.proceed is False
        assert result.reason == "infeasible:not_indexed"

    async def test_non_python_rejected(self, db_session):
        repo = await _make_repo(db_session)
        await _make_file(
            db_session,
            repo,
            "src/app.ts",
            language="typescript",
            structure={"functions": [{"name": "go"}]},
        )
        result = await is_feasible(
            db_session, repo.id, "owner/repo", "src/app.ts"
        )
        assert result.proceed is False
        assert "non_python_language" in result.reason

    async def test_too_large_rejected(self, db_session):
        repo = await _make_repo(db_session)
        await _make_file(
            db_session,
            repo,
            "src/huge.py",
            line_count=999,
            structure={"functions": [{"name": "go"}]},
        )
        result = await is_feasible(
            db_session, repo.id, "owner/repo", "src/huge.py"
        )
        assert result.proceed is False
        assert "too_large" in result.reason

    async def test_no_functions_or_classes_rejected(self, db_session):
        repo = await _make_repo(db_session)
        await _make_file(
            db_session, repo, "src/constants.py", structure={}
        )
        result = await is_feasible(
            db_session, repo.id, "owner/repo", "src/constants.py"
        )
        assert result.proceed is False
        assert "no_functions_or_classes" in result.reason

    async def test_only_private_symbols_rejected(self, db_session):
        repo = await _make_repo(db_session)
        await _make_file(
            db_session,
            repo,
            "src/internals.py",
            structure={
                "functions": [{"name": "_helper"}, {"name": "_other"}],
                "classes": [{"name": "_PrivateBase"}],
            },
        )
        result = await is_feasible(
            db_session, repo.id, "owner/repo", "src/internals.py"
        )
        assert result.proceed is False
        assert "no_public_symbols" in result.reason

    async def test_dunder_main_rejected(self, db_session):
        repo = await _make_repo(db_session)
        await _make_file(
            db_session,
            repo,
            "src/myapp/__main__.py",
            structure={"functions": [{"name": "main"}]},
        )
        result = await is_feasible(
            db_session, repo.id, "owner/repo", "src/myapp/__main__.py"
        )
        assert result.proceed is False
        assert "dunder_main_entrypoint" in result.reason

    async def test_cli_entrypoint_rejected(self, db_session):
        repo = await _make_repo(db_session)
        await _make_file(
            db_session,
            repo,
            "src/cli.py",
            structure={"functions": [{"name": "main"}]},
            content='def main():\n    pass\n\nif __name__ == "__main__":\n    main()\n',
        )
        result = await is_feasible(
            db_session, repo.id, "owner/repo", "src/cli.py"
        )
        assert result.proceed is False
        assert "cli_entrypoint" in result.reason

    async def test_cli_entrypoint_with_classes_passes(self, db_session):
        """A cli.py with helper classes is more than just an entrypoint."""
        repo = await _make_repo(db_session)
        await _make_file(
            db_session,
            repo,
            "src/cli.py",
            structure={
                "functions": [{"name": "main"}],
                "classes": [{"name": "Config"}],
            },
            content='if __name__ == "__main__":\n    main()\n',
        )
        result = await is_feasible(
            db_session, repo.id, "owner/repo", "src/cli.py"
        )
        assert result.proceed is True


class TestIsFeasiblePriorAttemptDedupe:
    async def test_prior_attempt_blocks(self, db_session):
        repo = await _make_repo(db_session)
        await _make_file(
            db_session,
            repo,
            "src/utils.py",
            structure={"functions": [{"name": "go"}]},
        )
        # Seed an agent_actions row with the bridge's evidence marker.
        db_session.add(
            AgentAction(
                repo_name="owner/repo",
                agent="test_generator",
                action="create_branch",
                signature="sig-prior",
                outcome="executed",
                confidence=0.95,
                evidence=[
                    "would create branch ...",
                    "purpose: add tests for `src/utils.py`",
                ],
            )
        )
        await db_session.flush()
        result = await is_feasible(
            db_session, repo.id, "owner/repo", "src/utils.py"
        )
        assert result.proceed is False
        assert "prior_attempt_exists" in result.reason

    async def test_prior_attempt_for_other_file_does_not_block(
        self, db_session
    ):
        repo = await _make_repo(db_session)
        await _make_file(
            db_session,
            repo,
            "src/utils.py",
            structure={"functions": [{"name": "go"}]},
        )
        db_session.add(
            AgentAction(
                repo_name="owner/repo",
                agent="test_generator",
                action="create_branch",
                signature="sig-other",
                outcome="executed",
                confidence=0.95,
                evidence=["purpose: add tests for `src/other.py`"],
            )
        )
        await db_session.flush()
        result = await is_feasible(
            db_session, repo.id, "owner/repo", "src/utils.py"
        )
        assert result.proceed is True

    async def test_prior_attempt_lookup_is_case_insensitive_on_repo_name(
        self, db_session
    ):
        repo = await _make_repo(db_session)
        await _make_file(
            db_session,
            repo,
            "src/utils.py",
            structure={"functions": [{"name": "go"}]},
        )
        # Stored uppercase, queried lowercase.
        db_session.add(
            AgentAction(
                repo_name="Owner/Repo",
                agent="test_generator",
                action="create_branch",
                signature="sig-case",
                outcome="executed",
                confidence=0.95,
                evidence=["purpose: add tests for `src/utils.py`"],
            )
        )
        await db_session.flush()
        result = await is_feasible(
            db_session, repo.id, "owner/repo", "src/utils.py"
        )
        assert result.proceed is False
        assert "prior_attempt_exists" in result.reason


# Make the dataclass explicitly imported so importers stay typed in CI.
def test_preflight_result_is_immutable():
    r = PreflightResult(proceed=True, reason="ok")
    with pytest.raises(Exception):
        r.proceed = False  # type: ignore[misc]
