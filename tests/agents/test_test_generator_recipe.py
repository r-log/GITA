"""Tests for the test-generator recipe (Week 8 Day 4).

Split into three layers:

1. **Path helpers** — pure, no I/O.
2. **Verification gates** — subprocess-driven, use ``tmp_path``. Each
   gate is covered in isolation (valid input, broken input).
3. **run_test_generation end-to-end** — wired against the indexed
   synthetic_py fixture with a ``FakeLLMClient``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gita.agents.test_generator.recipe import (
    TestGenerationResult,
    derive_test_file_path,
    run_test_generation,
    verify_test_file,
)
from gita.agents.test_generator.schemas import GeneratedTestResponse
from gita.llm.client import FakeLLMClient


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
class TestDeriveTestFilePath:
    def test_stem_becomes_test_prefix(self):
        assert derive_test_file_path("src/myapp/utils.py") == (
            "tests/test_utils.py"
        )

    def test_already_in_tests_dir(self):
        """Not our concern — the default is always tests/test_<stem>."""
        assert derive_test_file_path("tests/test_foo.py") == (
            "tests/test_test_foo.py"
        )

    def test_file_without_extension(self):
        assert derive_test_file_path("scripts/build") == (
            "tests/test_build.py"
        )


# ---------------------------------------------------------------------------
# Verification gates
# ---------------------------------------------------------------------------
_VALID_TEST_FILE = """\
import pytest


def test_addition():
    assert 1 + 1 == 2


@pytest.mark.parametrize("x,y", [(1, 2), (2, 3)])
def test_less_than(x, y):
    assert x < y
"""

_BROKEN_SYNTAX = """\
def test_bad(:   # <-- syntax error
    assert True
"""

_BAD_IMPORT = """\
import this_module_definitely_does_not_exist_anywhere_xyz


def test_ok():
    assert True
"""

_NO_TESTS = """\
# This file has no test_ functions.
x = 1
"""


class TestVerifyAstGate:
    async def test_valid_passes(self, tmp_path: Path):
        verified, errors = await verify_test_file(
            _VALID_TEST_FILE, tmp_path, "test_sample.py"
        )
        assert verified is True
        assert errors == []

    async def test_broken_syntax_fails_at_ast(self, tmp_path: Path):
        verified, errors = await verify_test_file(
            _BROKEN_SYNTAX, tmp_path, "test_sample.py"
        )
        assert verified is False
        assert len(errors) == 1
        assert "ast.parse failed" in errors[0]
        # File should NOT have been written — ast gate is in-process
        # and returns before the filesystem drop.
        assert not (tmp_path / "test_sample.py").exists()


class TestVerifyPytestGate:
    async def test_import_error_fails_collection(self, tmp_path: Path):
        verified, errors = await verify_test_file(
            _BAD_IMPORT, tmp_path, "test_sample.py"
        )
        assert verified is False
        assert any(
            "pytest --collect-only failed" in e for e in errors
        )

    async def test_no_tests_fails_collection(self, tmp_path: Path):
        """pytest exits 5 on ``no tests collected`` — we treat that
        as a failure because the generator's job is to produce tests."""
        verified, errors = await verify_test_file(
            _NO_TESTS, tmp_path, "test_sample.py"
        )
        assert verified is False

    async def test_test_file_written_for_subprocess_gates(
        self, tmp_path: Path
    ):
        """The subprocess gates need the file on disk; verify that."""
        await verify_test_file(
            _VALID_TEST_FILE, tmp_path, "subdir/test_sample.py"
        )
        assert (tmp_path / "subdir" / "test_sample.py").exists()


# ---------------------------------------------------------------------------
# run_test_generation — end-to-end with fake LLM + real DB
# ---------------------------------------------------------------------------
def _fake_response(
    *,
    content: str = _VALID_TEST_FILE,
    symbols: list[str] | None = None,
    notes: str = "",
    confidence: float = 0.85,
) -> GeneratedTestResponse:
    return GeneratedTestResponse(
        test_file_content=content,
        covered_symbols=symbols or ["format_name", "validate_email"],
        notes=notes,
        confidence=confidence,
    )


class TestRunTestGenerationHappyPath:
    async def test_produces_verified_result(
        self, indexed_synth_py, tmp_path: Path
    ):
        session, repo_name = indexed_synth_py
        fake = FakeLLMClient(responses=[_fake_response()])

        result = await run_test_generation(
            session,
            repo_name,
            "src/myapp/utils.py",
            llm=fake,
            repo_root=tmp_path,
        )

        assert isinstance(result, TestGenerationResult)
        assert result.verified is True
        assert result.verification_errors == []
        assert result.target_file == "src/myapp/utils.py"
        assert result.test_file_path == "tests/test_utils.py"
        assert result.test_content == _VALID_TEST_FILE
        assert result.covered_symbols == [
            "format_name",
            "validate_email",
        ]

    async def test_makes_exactly_one_llm_call(
        self, indexed_synth_py, tmp_path: Path
    ):
        session, repo_name = indexed_synth_py
        fake = FakeLLMClient(responses=[_fake_response()])

        await run_test_generation(
            session,
            repo_name,
            "src/myapp/utils.py",
            llm=fake,
            repo_root=tmp_path,
        )

        assert len(fake.calls) == 1
        assert fake.calls[0]["schema"] == "GeneratedTestResponse"

    async def test_prompt_includes_target_source(
        self, indexed_synth_py, tmp_path: Path
    ):
        """Minimal signal that context was built from the index, not
        hallucinated — the source of the target file must appear in
        the user prompt."""
        session, repo_name = indexed_synth_py
        fake = FakeLLMClient(responses=[_fake_response()])

        await run_test_generation(
            session,
            repo_name,
            "src/myapp/utils.py",
            llm=fake,
            repo_root=tmp_path,
        )

        user = fake.calls[0]["user"]
        assert "def format_name" in user
        assert "def validate_email" in user

    async def test_verified_bonus_lifts_confidence(
        self, indexed_synth_py, tmp_path: Path
    ):
        """LLM says 0.85; verified → +0.05 bonus."""
        session, repo_name = indexed_synth_py
        fake = FakeLLMClient(responses=[_fake_response(confidence=0.85)])

        result = await run_test_generation(
            session,
            repo_name,
            "src/myapp/utils.py",
            llm=fake,
            repo_root=tmp_path,
        )

        assert result.llm_confidence == 0.85
        assert result.verified is True
        assert 0.89 < result.confidence <= 0.91


class TestRunTestGenerationFailurePaths:
    async def test_unknown_file_raises(
        self, indexed_synth_py, tmp_path: Path
    ):
        session, repo_name = indexed_synth_py
        fake = FakeLLMClient(responses=[_fake_response()])

        with pytest.raises(FileNotFoundError, match="not found in index"):
            await run_test_generation(
                session,
                repo_name,
                "src/myapp/does_not_exist.py",
                llm=fake,
                repo_root=tmp_path,
            )

    async def test_broken_syntax_fails_verification(
        self, indexed_synth_py, tmp_path: Path
    ):
        session, repo_name = indexed_synth_py
        fake = FakeLLMClient(
            responses=[
                _fake_response(content=_BROKEN_SYNTAX, confidence=0.9)
            ]
        )

        result = await run_test_generation(
            session,
            repo_name,
            "src/myapp/utils.py",
            llm=fake,
            repo_root=tmp_path,
        )

        assert result.verified is False
        assert any(
            "ast.parse failed" in e for e in result.verification_errors
        )
        # Content is still returned so the caller can display it.
        assert result.test_content == _BROKEN_SYNTAX

    async def test_unverified_confidence_pinned_to_ceiling(
        self, indexed_synth_py, tmp_path: Path
    ):
        """LLM reports 0.95, but verification fails → blended ≤ 0.4.
        That ceiling guarantees the bridge's Decisions auto-downgrade
        well below the 0.9 code-action threshold without any
        recipe-specific logic."""
        session, repo_name = indexed_synth_py
        fake = FakeLLMClient(
            responses=[
                _fake_response(content=_BAD_IMPORT, confidence=0.95)
            ]
        )

        result = await run_test_generation(
            session,
            repo_name,
            "src/myapp/utils.py",
            llm=fake,
            repo_root=tmp_path,
        )

        assert result.verified is False
        assert result.llm_confidence == 0.95
        assert result.confidence <= 0.4


class TestRunTestGenerationCustomPath:
    async def test_override_test_file_path(
        self, indexed_synth_py, tmp_path: Path
    ):
        """AMASS-style tests live in ``backend/tests/``, not ``tests/``.
        The caller can override the default path to match a repo's
        layout. The override flows through to the result, but Week 9's
        scratch-dir refactor means nothing lands in ``repo_root`` —
        verification writes into a private tempdir that's deleted
        before the recipe returns.
        """
        session, repo_name = indexed_synth_py
        fake = FakeLLMClient(responses=[_fake_response()])

        result = await run_test_generation(
            session,
            repo_name,
            "src/myapp/utils.py",
            llm=fake,
            repo_root=tmp_path,
            test_file_path="backend/tests/unit/test_utils.py",
        )

        assert result.test_file_path == "backend/tests/unit/test_utils.py"
        # The repo_root must stay clean — Week 9 guarantee.
        assert not (
            tmp_path / "backend" / "tests" / "unit" / "test_utils.py"
        ).exists()
        assert list(tmp_path.iterdir()) == []


class TestRunTestGenerationScratchIsolation:
    async def test_repo_root_is_not_mutated_on_success(
        self, indexed_synth_py, tmp_path: Path
    ):
        """Week 9: even when verification writes files for the
        subprocess gates, none of those writes hit ``repo_root``."""
        session, repo_name = indexed_synth_py
        fake = FakeLLMClient(responses=[_fake_response()])

        result = await run_test_generation(
            session,
            repo_name,
            "src/myapp/utils.py",
            llm=fake,
            repo_root=tmp_path,
        )

        assert result.verified is True
        assert list(tmp_path.iterdir()) == []

    async def test_repo_root_is_not_mutated_on_verification_failure(
        self, indexed_synth_py, tmp_path: Path
    ):
        session, repo_name = indexed_synth_py
        fake = FakeLLMClient(
            responses=[_fake_response(content=_BAD_IMPORT, confidence=0.95)]
        )

        result = await run_test_generation(
            session,
            repo_name,
            "src/myapp/utils.py",
            llm=fake,
            repo_root=tmp_path,
        )

        assert result.verified is False
        assert list(tmp_path.iterdir()) == []
