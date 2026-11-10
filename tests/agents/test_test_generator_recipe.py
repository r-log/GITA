"""Tests for the test-generator recipe.

Layers:

1. **Path helpers and confidence policy** — pure.
2. **Static checks** — in-process AST checks; nothing executed.
3. **verify_test_file** — runs generated tests in a ``LocalSandbox``
   against a throwaway copy of the synthetic_py fixture repo.
4. **run_test_generation end-to-end** — wired against the indexed
   synthetic_py fixture with a ``FakeLLMClient``.

Docker-backed isolation is covered in ``test_test_generator_sandbox.py``.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from gita.agents.test_generator.recipe import (
    TestGenerationResult,
    blend_confidence,
    derive_test_file_path,
    run_test_generation,
    static_checks,
    verify_test_file,
)
from gita.agents.test_generator.sandbox import InvalidTestPathError, LocalSandbox
from gita.agents.test_generator.schemas import GeneratedTestResponse
from gita.llm.client import FakeLLMClient

_FIXTURE_REPO = Path(__file__).parent.parent / "fixtures" / "synthetic_py"
_TARGET = "src/myapp/utils.py"


@pytest.fixture
def synth_repo(tmp_path: Path) -> Path:
    """A private copy of the synthetic_py repo (tests may inspect it)."""
    repo = tmp_path / "repo"
    shutil.copytree(_FIXTURE_REPO, repo)
    return repo


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# ---------------------------------------------------------------------------
# Generated-test fixtures
# ---------------------------------------------------------------------------
_PASSING = """\
import pytest

from myapp.utils import format_name, validate_email


def test_format_name_strips_and_titles():
    assert format_name("  ada lovelace ") == "Ada Lovelace"


@pytest.mark.parametrize("email,ok", [("a@b.io", True), ("nope", False)])
def test_validate_email(email, ok):
    assert validate_email(email) is ok
"""

# Imports and collects fine, but the assertion is false. The old
# "collect-only" verification marked this verified.
_FAILING = """\
from myapp.utils import format_name


def test_wrong_expectation():
    assert format_name("ada") == "ADA"
"""

_BROKEN_SYNTAX = """\
def test_bad(:   # <-- syntax error
    assert True
"""

_NO_TARGET_IMPORT = """\
def test_trivial():
    assert 1 + 1 == 2
"""

_NO_ASSERTS = """\
from myapp.utils import format_name


def test_calls_only():
    format_name("x")
"""

_NO_TESTS = """\
from myapp.utils import format_name

x = format_name("y")
assert x
"""


def _fake_response(
    *,
    content: str = _PASSING,
    confidence: float = 0.92,
) -> GeneratedTestResponse:
    return GeneratedTestResponse(
        test_file_content=content,
        covered_symbols=["format_name", "validate_email"],
        notes="",
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Path helpers + confidence policy
# ---------------------------------------------------------------------------
class TestDeriveTestFilePath:
    def test_stem_becomes_test_prefix(self):
        assert derive_test_file_path("src/myapp/utils.py") == "tests/test_utils.py"

    def test_file_without_extension(self):
        assert derive_test_file_path("scripts/run") == "tests/test_run.py"


class TestBlendConfidence:
    def test_model_cannot_open_the_gate_alone(self):
        """A confident model with failing tests stays under the 0.9 bar."""
        assert blend_confidence(0.99, verified=False) == 0.4

    def test_model_can_veto_passing_tests(self):
        assert blend_confidence(0.5, verified=True) == 0.5

    def test_verified_is_capped_below_certainty(self):
        assert blend_confidence(1.0, verified=True) == 0.95

    def test_out_of_range_self_report_is_clamped(self):
        assert blend_confidence(7.0, verified=True) == 0.95
        assert blend_confidence(-1.0, verified=True) == 0.0


# ---------------------------------------------------------------------------
# Static checks
# ---------------------------------------------------------------------------
class TestStaticChecks:
    names = ["myapp.utils"]

    def test_passing_file_has_no_errors(self):
        assert static_checks(_PASSING, self.names) == []

    def test_syntax_error(self):
        errors = static_checks(_BROKEN_SYNTAX, self.names)
        assert len(errors) == 1 and "ast.parse failed" in errors[0]

    def test_must_import_target_module(self):
        errors = static_checks(_NO_TARGET_IMPORT, self.names)
        assert any("does not import the target module" in e for e in errors)

    def test_parent_package_import_counts(self):
        content = "from myapp import utils\n\ndef test_x():\n    assert utils\n"
        assert static_checks(content, self.names) == []

    def test_must_assert_something(self):
        errors = static_checks(_NO_ASSERTS, self.names)
        assert errors == ["test file makes no assertions"]

    def test_pytest_raises_counts_as_assertion(self):
        content = (
            "import pytest\nfrom myapp.utils import format_name\n\n"
            "def test_x():\n    with pytest.raises(AttributeError):\n"
            "        format_name(None)\n"
        )
        assert static_checks(content, self.names) == []

    def test_must_define_tests(self):
        errors = static_checks(_NO_TESTS, self.names)
        assert any("no test functions" in e for e in errors)

    def test_unknown_import_name_fails_closed(self):
        errors = static_checks(_PASSING, [])
        assert any("could not determine" in e for e in errors)


# ---------------------------------------------------------------------------
# verify_test_file — real pytest runs in a LocalSandbox
# ---------------------------------------------------------------------------
async def _verify(content: str, repo: Path, **kwargs):
    return await verify_test_file(
        content,
        repo_root=repo,
        test_file_path=kwargs.pop("test_file_path", "tests/test_utils.py"),
        target_file=_TARGET,
        sandbox=kwargs.pop("sandbox", LocalSandbox()),
        **kwargs,
    )


class TestVerifyTestFile:
    async def test_passing_tests_are_verified(self, synth_repo: Path):
        report = await _verify(_PASSING, synth_repo)
        assert report.verified is True, report.errors
        assert report.tests_passed == 3
        assert report.tests_failed == 0
        assert report.sandbox == "local"

    async def test_failing_assertion_is_not_verified(self, synth_repo: Path):
        """The regression: collect-only verification passed this file."""
        report = await _verify(_FAILING, synth_repo)
        assert report.verified is False
        assert report.tests_failed == 1
        assert "1 test(s) failed" in report.errors[0]

    async def test_static_failure_runs_nothing(self, synth_repo: Path):
        report = await _verify(_NO_TARGET_IMPORT, synth_repo)
        assert report.verified is False
        assert report.tests_passed == 0

    async def test_import_error_is_not_verified(self, synth_repo: Path):
        content = (
            "import this_module_does_not_exist_xyz\n"
            "from myapp.utils import format_name\n\n"
            "def test_x():\n    assert format_name('a') == 'A'\n"
        )
        report = await _verify(content, synth_repo)
        assert report.verified is False
        assert "could not collect" in report.errors[0]

    async def test_require_isolation_refuses_local_sandbox(self, synth_repo: Path):
        report = await _verify(_PASSING, synth_repo, require_isolation=True)
        assert report.verified is False
        assert "not isolated" in report.errors[0]
        assert report.tests_passed == 0

    async def test_repo_is_never_mutated(self, synth_repo: Path):
        before = _snapshot(synth_repo)
        await _verify(_PASSING, synth_repo)
        await _verify(_FAILING, synth_repo)
        assert _snapshot(synth_repo) == before

    async def test_path_traversal_rejected(self, synth_repo: Path):
        with pytest.raises(InvalidTestPathError):
            await _verify(_PASSING, synth_repo, test_file_path="../../x_test.py")


# ---------------------------------------------------------------------------
# run_test_generation — end-to-end with fake LLM + real DB
# ---------------------------------------------------------------------------
async def _run(session, repo_name, repo: Path, fake, **kwargs):
    return await run_test_generation(
        session,
        repo_name,
        kwargs.pop("target_file", _TARGET),
        llm=fake,
        repo_root=repo,
        sandbox=kwargs.pop("sandbox", LocalSandbox()),
        **kwargs,
    )


class TestRunTestGeneration:
    async def test_produces_verified_result(self, indexed_synth_py, synth_repo):
        session, repo_name = indexed_synth_py
        fake = FakeLLMClient(responses=[_fake_response()])

        result = await _run(session, repo_name, synth_repo, fake)

        assert isinstance(result, TestGenerationResult)
        assert result.verified is True, result.verification_errors
        assert result.tests_passed == 3
        assert result.test_file_path == "tests/test_utils.py"
        assert result.confidence == pytest.approx(0.92)
        assert len(fake.calls) == 1
        assert fake.calls[0]["schema"] == "GeneratedTestResponse"

    async def test_prompt_carries_source_and_import_path(
        self, indexed_synth_py, synth_repo
    ):
        session, repo_name = indexed_synth_py
        fake = FakeLLMClient(responses=[_fake_response()])

        await _run(session, repo_name, synth_repo, fake)

        user = fake.calls[0]["user"]
        assert "def format_name" in user
        assert "`myapp.utils`" in user
        # Repository content is fenced off as data.
        assert "<source>" in user and "</source>" in user

    async def test_confident_model_with_failing_tests_stays_below_gate(
        self, indexed_synth_py, synth_repo
    ):
        """The model said 0.99; the tests fail; the 0.9 write gate stays shut."""
        session, repo_name = indexed_synth_py
        fake = FakeLLMClient(
            responses=[_fake_response(content=_FAILING, confidence=0.99)]
        )

        result = await _run(session, repo_name, synth_repo, fake)

        assert result.verified is False
        assert result.llm_confidence == 0.99
        assert result.confidence == 0.4
        assert result.test_content == _FAILING  # still returned for display

    async def test_unknown_file_raises(self, indexed_synth_py, synth_repo):
        session, repo_name = indexed_synth_py
        fake = FakeLLMClient(responses=[_fake_response()])
        with pytest.raises(FileNotFoundError, match="not found in index"):
            await _run(
                session,
                repo_name,
                synth_repo,
                fake,
                target_file="src/myapp/does_not_exist.py",
            )

    async def test_custom_test_path_flows_through_without_touching_repo(
        self, indexed_synth_py, synth_repo
    ):
        session, repo_name = indexed_synth_py
        fake = FakeLLMClient(responses=[_fake_response()])
        before = _snapshot(synth_repo)

        result = await _run(
            session,
            repo_name,
            synth_repo,
            fake,
            test_file_path="backend/tests/unit/test_utils.py",
        )

        assert result.verified is True, result.verification_errors
        assert result.test_file_path == "backend/tests/unit/test_utils.py"
        assert _snapshot(synth_repo) == before

    async def test_non_test_path_rejected_before_llm_call(
        self, indexed_synth_py, synth_repo
    ):
        """A path that would overwrite source code never reaches the LLM."""
        session, repo_name = indexed_synth_py
        fake = FakeLLMClient(responses=[_fake_response()])
        with pytest.raises(InvalidTestPathError):
            await _run(
                session,
                repo_name,
                synth_repo,
                fake,
                test_file_path="src/myapp/utils.py",
            )
        assert fake.calls == []
