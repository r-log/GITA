"""Tests for architectural guardrails.

Each guardrail is a wall, not a rule: these tests prove that findings
fail structurally regardless of what the LLM claims. The LLM could
produce a finding with 0.99 confidence on a hallucinated file path,
and the guardrail drops it before it reaches the milestone grouping
stage or the Decision bridge.

Test structure:
- Pure unit tests for the regex/parse helpers (no DB)
- DB-integrated tests for ``verify_findings`` (needs ``db_session``)
- Unit tests for ``structural_confidence`` (pure math)
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gita.agents.guardrails import (
    _BANNED_DESCRIPTION_PATTERNS,
    _check_banned_phrase,
    _claims_syntax_error,
    _file_parses_cleanly,
    structural_confidence,
    verify_findings,
)
from gita.agents.types import Finding
from gita.db.models import CodeIndex, Repo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _finding(
    file: str = "src/app.py",
    line: int = 23,
    severity: str = "high",
    kind: str = "bug",
    description: str = "something bad",
    fix_sketch: str = "",
) -> Finding:
    return Finding(
        file=file,
        line=line,
        severity=severity,
        kind=kind,
        description=description,
        fix_sketch=fix_sketch,
    )


async def _seed_repo_with_files(
    session: AsyncSession,
    files: dict[str, tuple[str, int, str]],
) -> uuid.UUID:
    """Seed a repo with files. ``files`` is ``{path: (language, line_count, content)}``."""
    repo = Repo(name="test_repo", root_path="/tmp/test")
    session.add(repo)
    await session.flush()
    for path, (language, line_count, content) in files.items():
        session.add(
            CodeIndex(
                repo_id=repo.id,
                file_path=path,
                language=language,
                line_count=line_count,
                content=content,
                structure={},
            )
        )
    await session.flush()
    return repo.id


# ---------------------------------------------------------------------------
# Pure helpers: _claims_syntax_error
# ---------------------------------------------------------------------------
class TestClaimsSyntaxError:
    def test_matches_syntax_error(self):
        f = _finding(description="Syntax error on line 170: missing paren")
        assert _claims_syntax_error(f) is True

    def test_matches_unclosed_paren(self):
        f = _finding(description="unclosed parenthesis in getattr call")
        assert _claims_syntax_error(f) is True

    def test_matches_unparseable(self):
        f = _finding(description="This code is unparseable by the interpreter")
        assert _claims_syntax_error(f) is True

    def test_matches_will_not_run(self):
        f = _finding(description="The module will not run due to missing bracket")
        assert _claims_syntax_error(f) is True

    def test_matches_missing_closing_paren(self):
        f = _finding(description="Missing closing parenthesis after dict literal")
        assert _claims_syntax_error(f) is True

    def test_does_not_match_normal_bug(self):
        f = _finding(description="SQL injection via f-string interpolation")
        assert _claims_syntax_error(f) is False

    def test_does_not_match_mutable_default(self):
        f = _finding(description="mutable default argument roles=[]")
        assert _claims_syntax_error(f) is False


# ---------------------------------------------------------------------------
# Pure helpers: _check_banned_phrase
# ---------------------------------------------------------------------------
class TestCheckBannedPhrase:
    def test_add_unit_tests_banned(self):
        f = _finding(description="You should add unit tests for this module")
        assert _check_banned_phrase(f) is not None

    def test_set_up_ci_banned(self):
        f = _finding(description="Set up CI/CD pipeline for deployment")
        assert _check_banned_phrase(f) is not None

    def test_improve_code_quality_banned(self):
        f = _finding(description="Improve code quality across the module")
        assert _check_banned_phrase(f) is not None

    def test_add_documentation_banned(self):
        f = _finding(description="Add more documentation to the API")
        assert _check_banned_phrase(f) is not None

    def test_fix_sketch_also_checked(self):
        f = _finding(
            description="Something legitimate",
            fix_sketch="Add tests to cover this edge case",
        )
        assert _check_banned_phrase(f) is not None

    def test_real_bug_not_banned(self):
        f = _finding(description="SQL injection via f-string on line 42")
        assert _check_banned_phrase(f) is None

    def test_patterns_loaded(self):
        assert len(_BANNED_DESCRIPTION_PATTERNS) >= 8


# ---------------------------------------------------------------------------
# Pure helpers: _file_parses_cleanly
# ---------------------------------------------------------------------------
class TestFileParsesCleanly:
    def test_valid_python_parses(self):
        code = "def foo():\n    return 42\n"
        assert _file_parses_cleanly(code, "python") is True

    def test_valid_multiline_getattr_parses(self):
        """The exact P6 pattern that fooled the LLM."""
        code = (
            "user_id = getattr(request, 'current_user', {}\n"
            "                  ).get('user_id', 'anonymous')\n"
        )
        assert _file_parses_cleanly(code, "python") is True

    def test_actually_broken_python_does_not_parse(self):
        code = "def foo(\n"  # genuinely unclosed
        assert _file_parses_cleanly(code, "python") is False

    def test_non_python_gets_benefit_of_doubt(self):
        """JS/TS files can't be AST-checked yet — we don't drop them."""
        code = "definitely not { valid json ["
        assert _file_parses_cleanly(code, "javascript") is True
        assert _file_parses_cleanly(code, "typescript") is True


# ---------------------------------------------------------------------------
# DB-integrated: verify_findings
# ---------------------------------------------------------------------------
class TestVerifyFindingsFileExistence:
    async def test_existing_file_passes(self, db_session: AsyncSession):
        repo_id = await _seed_repo_with_files(db_session, {
            "src/app.py": ("python", 100, "x = 1\n" * 100),
        })
        findings = [_finding(file="src/app.py", line=50)]
        verified, dropped = await verify_findings(
            db_session, repo_id, findings
        )
        assert len(verified) == 1
        assert len(dropped) == 0

    async def test_nonexistent_file_dropped(self, db_session: AsyncSession):
        repo_id = await _seed_repo_with_files(db_session, {
            "src/app.py": ("python", 100, "x = 1\n" * 100),
        })
        findings = [_finding(file="src/ghost.py", line=1)]
        verified, dropped = await verify_findings(
            db_session, repo_id, findings
        )
        assert len(verified) == 0
        assert len(dropped) == 1
        assert "file_not_found" in dropped[0][1]


class TestVerifyFindingsLineRange:
    async def test_in_range_passes(self, db_session: AsyncSession):
        repo_id = await _seed_repo_with_files(db_session, {
            "src/app.py": ("python", 50, "x = 1\n" * 50),
        })
        findings = [_finding(file="src/app.py", line=50)]
        verified, _ = await verify_findings(
            db_session, repo_id, findings
        )
        assert len(verified) == 1

    async def test_out_of_range_dropped(self, db_session: AsyncSession):
        repo_id = await _seed_repo_with_files(db_session, {
            "src/app.py": ("python", 50, "x = 1\n" * 50),
        })
        findings = [_finding(file="src/app.py", line=999)]
        verified, dropped = await verify_findings(
            db_session, repo_id, findings
        )
        assert len(verified) == 0
        assert "line_out_of_range" in dropped[0][1]


class TestVerifyFindingsASTGate:
    async def test_syntax_claim_on_valid_file_dropped(
        self, db_session: AsyncSession
    ):
        """The P6 wall: LLM claims syntax error, file parses cleanly → drop."""
        valid_python = (
            "user_id = getattr(request, 'current_user', {}\n"
            "                  ).get('user_id', 'anonymous')\n"
        )
        repo_id = await _seed_repo_with_files(db_session, {
            "decorators.py": ("python", 2, valid_python),
        })
        findings = [
            _finding(
                file="decorators.py",
                line=1,
                description="Syntax error: unclosed parenthesis in getattr call",
            )
        ]
        verified, dropped = await verify_findings(
            db_session, repo_id, findings
        )
        assert len(verified) == 0
        assert "syntax_claim_on_valid_file" in dropped[0][1]

    async def test_syntax_claim_on_genuinely_broken_file_passes(
        self, db_session: AsyncSession
    ):
        """If the file actually HAS a syntax error, the finding survives."""
        broken_python = "def foo(\n"
        repo_id = await _seed_repo_with_files(db_session, {
            "broken.py": ("python", 1, broken_python),
        })
        findings = [
            _finding(
                file="broken.py",
                line=1,
                description="Syntax error: unclosed parenthesis",
            )
        ]
        verified, dropped = await verify_findings(
            db_session, repo_id, findings
        )
        assert len(verified) == 1
        assert len(dropped) == 0

    async def test_non_syntax_finding_on_valid_file_passes(
        self, db_session: AsyncSession
    ):
        """A real bug finding (not claiming syntax error) isn't affected."""
        repo_id = await _seed_repo_with_files(db_session, {
            "db.py": ("python", 50, "x = 1\n" * 50),
        })
        findings = [
            _finding(
                file="db.py",
                line=10,
                description="SQL injection via f-string interpolation",
            )
        ]
        verified, _ = await verify_findings(
            db_session, repo_id, findings
        )
        assert len(verified) == 1


class TestVerifyFindingsBannedPhrase:
    async def test_boilerplate_finding_dropped(
        self, db_session: AsyncSession
    ):
        repo_id = await _seed_repo_with_files(db_session, {
            "src/app.py": ("python", 100, "x = 1\n" * 100),
        })
        findings = [
            _finding(
                file="src/app.py",
                line=1,
                description="You should add unit tests for this module",
            )
        ]
        verified, dropped = await verify_findings(
            db_session, repo_id, findings
        )
        assert len(verified) == 0
        assert "banned phrase" in dropped[0][1]


class TestVerifyFindingsMultiple:
    async def test_mixed_findings_filter_correctly(
        self, db_session: AsyncSession
    ):
        """Multiple findings, some good, some bad — only good survive."""
        repo_id = await _seed_repo_with_files(db_session, {
            "src/app.py": ("python", 100, "x = 1\n" * 100),
            "src/db.py": ("python", 50, "y = 2\n" * 50),
        })
        findings = [
            _finding(  # Good: real bug, valid file, valid line
                file="src/app.py", line=23,
                description="SQL injection via f-string",
            ),
            _finding(  # Bad: hallucinated file
                file="src/ghost.py", line=1,
                description="Missing error handling",
            ),
            _finding(  # Bad: line out of range
                file="src/db.py", line=999,
                description="Unreachable code",
            ),
            _finding(  # Bad: boilerplate
                file="src/app.py", line=10,
                description="Add unit tests to cover edge cases",
            ),
            _finding(  # Good: real bug, valid file, valid line
                file="src/db.py", line=42,
                description="Hardcoded password on line 42",
            ),
        ]
        verified, dropped = await verify_findings(
            db_session, repo_id, findings
        )
        assert len(verified) == 2
        assert len(dropped) == 3
        assert verified[0].description == "SQL injection via f-string"
        assert verified[1].description == "Hardcoded password on line 42"

    async def test_empty_findings_returns_empty(
        self, db_session: AsyncSession
    ):
        repo_id = await _seed_repo_with_files(db_session, {
            "src/app.py": ("python", 10, "x = 1\n"),
        })
        verified, dropped = await verify_findings(
            db_session, repo_id, []
        )
        assert verified == []
        assert dropped == []


# ---------------------------------------------------------------------------
# Structural confidence
# ---------------------------------------------------------------------------
class TestStructuralConfidence:
    def test_all_verified_no_penalty(self):
        assert structural_confidence(5, 5, 0.9) == 0.9

    def test_half_filtered_halves_confidence(self):
        result = structural_confidence(10, 5, 0.8)
        assert result == pytest.approx(0.4)

    def test_all_filtered_bottoms_at_0_1(self):
        result = structural_confidence(5, 0, 0.9)
        assert result == 0.1

    def test_zero_originals_no_penalty(self):
        """No findings produced = nothing to filter, confidence unchanged."""
        assert structural_confidence(0, 0, 0.7) == 0.7

    def test_one_out_of_seven_filtered(self):
        """The Day 6 AMASS scenario: 7 findings, 1 dropped."""
        result = structural_confidence(7, 6, 0.87)
        expected = 0.87 * (6 / 7)
        assert result == pytest.approx(expected, abs=0.01)
