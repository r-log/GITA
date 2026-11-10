"""Tests for the test-generator preflight gates.

Stage A (``has_existing_tests``) is sync, filesystem-only — uses
``tmp_path``-driven mini-repos. Stage B (``is_feasible``) is async
and DB-backed — uses the existing ``indexed_synth_py`` fixture for
the happy path and seeds extra rows for the edge cases.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from gita.agents.test_generator.preflight import (
    SUPPORTED_PREVIEW_LANGUAGES,
    PreflightResult,
    has_existing_tests,
    is_feasible,
    preview_repo,
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
    async def test_python_file_with_public_function_passes(self, db_session):
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
        result = await is_feasible(db_session, repo.id, "owner/repo", "src/utils.py")
        assert result.proceed is True
        assert result.reason == "ok"


class TestIsFeasibleRejections:
    async def test_missing_from_index_rejected(self, db_session):
        repo = await _make_repo(db_session)
        result = await is_feasible(db_session, repo.id, "owner/repo", "src/missing.py")
        assert result.proceed is False
        assert result.reason == "infeasible:not_indexed"

    async def test_non_python_rejected_by_default(self, db_session):
        """With the recipe Python-only, TS files are rejected with the
        new ``unsupported_language`` reason (the auto-trigger relies on
        the default to skip enqueueing JS/TS jobs)."""
        repo = await _make_repo(db_session)
        await _make_file(
            db_session,
            repo,
            "src/app.ts",
            language="typescript",
            structure={"functions": [{"name": "go"}]},
        )
        result = await is_feasible(db_session, repo.id, "owner/repo", "src/app.ts")
        assert result.proceed is False
        assert "unsupported_language" in result.reason
        assert "typescript" in result.reason

    async def test_too_large_rejected(self, db_session):
        repo = await _make_repo(db_session)
        await _make_file(
            db_session,
            repo,
            "src/huge.py",
            line_count=999,
            structure={"functions": [{"name": "go"}]},
        )
        result = await is_feasible(db_session, repo.id, "owner/repo", "src/huge.py")
        assert result.proceed is False
        assert "too_large" in result.reason

    async def test_no_functions_or_classes_rejected(self, db_session):
        repo = await _make_repo(db_session)
        await _make_file(db_session, repo, "src/constants.py", structure={})
        result = await is_feasible(db_session, repo.id, "owner/repo", "src/constants.py")
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
        result = await is_feasible(db_session, repo.id, "owner/repo", "src/internals.py")
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
        result = await is_feasible(db_session, repo.id, "owner/repo", "src/myapp/__main__.py")
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
        result = await is_feasible(db_session, repo.id, "owner/repo", "src/cli.py")
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
        result = await is_feasible(db_session, repo.id, "owner/repo", "src/cli.py")
        assert result.proceed is True


class TestIsFeasiblePriorAttemptDedupe:
    @pytest.fixture(autouse=True)
    def _live_mode(self, monkeypatch):
        # The seeded rows are executed (live) actions.
        monkeypatch.setattr("gita.config.settings.write_mode", "full")

    async def test_shadow_attempt_does_not_block_live_generation(self, db_session, monkeypatch):
        repo = await _make_repo(db_session)
        await _make_file(
            db_session, repo, "src/utils.py", structure={"functions": [{"name": "go"}]}
        )
        db_session.add(
            AgentAction(
                repo_name="owner/repo",
                agent="test_generator",
                action="create_branch",
                signature="sig-shadow",
                outcome="shadow_logged",
                confidence=0.95,
                evidence=["purpose: add tests for `src/utils.py`"],
                live=False,
            )
        )
        await db_session.flush()

        live = await is_feasible(db_session, repo.id, "owner/repo", "src/utils.py")
        assert live.proceed is True

        monkeypatch.setattr("gita.config.settings.write_mode", "shadow")
        shadow = await is_feasible(db_session, repo.id, "owner/repo", "src/utils.py")
        assert shadow.proceed is False

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
        result = await is_feasible(db_session, repo.id, "owner/repo", "src/utils.py")
        assert result.proceed is False
        assert "prior_attempt_exists" in result.reason

    async def test_prior_attempt_for_other_file_does_not_block(self, db_session):
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
        result = await is_feasible(db_session, repo.id, "owner/repo", "src/utils.py")
        assert result.proceed is True

    async def test_prior_attempt_lookup_is_case_insensitive_on_repo_name(self, db_session):
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
        result = await is_feasible(db_session, repo.id, "owner/repo", "src/utils.py")
        assert result.proceed is False
        assert "prior_attempt_exists" in result.reason


# ---------------------------------------------------------------------------
# Stage A — TS/JS coverage
#
# Mirrors the Python checks above. Sibling and mirror-layout name
# patterns differ (`.test.<ext>` / `.spec.<ext>`); the content grep
# matches path-based imports rather than dotted module names. All cases
# below are filesystem-only — no DB, no LLM.
# ---------------------------------------------------------------------------
class TestHasExistingTestsTypeScript:
    def test_no_tests_proceeds(self, tmp_path: Path):
        _write(tmp_path / "src" / "utils.ts", "export const x = 1;\n")
        result = has_existing_tests(tmp_path, "src/utils.ts")
        assert result.proceed is True
        assert result.reason == "ok"

    def test_sibling_dot_test_blocks(self, tmp_path: Path):
        _write(tmp_path / "src" / "utils.ts")
        _write(tmp_path / "src" / "utils.test.ts")
        result = has_existing_tests(tmp_path, "src/utils.ts")
        assert result.proceed is False
        assert "tests_exist:sibling" in result.reason
        assert "utils.test.ts" in result.reason

    def test_sibling_dot_spec_blocks(self, tmp_path: Path):
        _write(tmp_path / "src" / "utils.ts")
        _write(tmp_path / "src" / "utils.spec.ts")
        result = has_existing_tests(tmp_path, "src/utils.ts")
        assert result.proceed is False
        assert "utils.spec.ts" in result.reason

    def test_tsx_with_plain_ts_test_blocks(self, tmp_path: Path):
        """react-testing-library convention: ``Button.test.ts`` next to
        ``Button.tsx``."""
        _write(tmp_path / "src" / "Button.tsx")
        _write(tmp_path / "src" / "Button.test.ts")
        result = has_existing_tests(tmp_path, "src/Button.tsx")
        assert result.proceed is False
        assert "Button.test.ts" in result.reason

    def test_underscores_dir_mirror_blocks(self, tmp_path: Path):
        _write(tmp_path / "src" / "utils.ts")
        _write(tmp_path / "src" / "__tests__" / "utils.test.ts")
        result = has_existing_tests(tmp_path, "src/utils.ts")
        assert result.proceed is False
        assert "tests_exist:in_test_dir" in result.reason

    def test_top_level_tests_dir_blocks(self, tmp_path: Path):
        _write(tmp_path / "src" / "utils.ts")
        _write(tmp_path / "tests" / "utils.test.ts")
        result = has_existing_tests(tmp_path, "src/utils.ts")
        assert result.proceed is False
        assert "tests_exist:in_test_dir" in result.reason

    def test_relative_import_in_test_blocks(self, tmp_path: Path):
        """A test file whose name doesn't match any sibling pattern but
        which imports the target via a relative path."""
        _write(tmp_path / "src" / "math.ts", "export const add = ...;\n")
        _write(
            tmp_path / "tests" / "arithmetic.test.ts",
            'import { add } from "../src/math";\n'
            'test("add", () => { expect(add(1,2)).toBe(3); });\n',
        )
        result = has_existing_tests(tmp_path, "src/math.ts")
        assert result.proceed is False
        assert "tests_exist:imports_target" in result.reason
        assert "math" in result.reason

    def test_alias_import_blocks(self, tmp_path: Path):
        """tsconfig path-aliased import (``@/utils/helpers``) still
        contains the basename — the loose stem match catches it. The
        test filename is intentionally non-conventional so the path
        scan misses and we exercise the content-grep path."""
        _write(tmp_path / "src" / "utils" / "helpers.ts")
        _write(
            tmp_path / "tests" / "smoke.test.ts",
            'import { fmt } from "@/utils/helpers";\n'
            'test("smoke", () => { expect(fmt).toBeDefined(); });\n',
        )
        result = has_existing_tests(tmp_path, "src/utils/helpers.ts")
        assert result.proceed is False
        assert "imports_target" in result.reason
        assert "helpers" in result.reason

    def test_dynamic_import_blocks(self, tmp_path: Path):
        _write(tmp_path / "src" / "lazy.ts")
        _write(
            tmp_path / "tests" / "lazy.test.ts",
            'const mod = await import("../src/lazy");\n',
        )
        result = has_existing_tests(tmp_path, "src/lazy.ts")
        assert result.proceed is False

    def test_require_in_test_blocks(self, tmp_path: Path):
        """``require()`` style for projects that mix CJS test files."""
        _write(tmp_path / "src" / "config.ts")
        _write(
            tmp_path / "src" / "__tests__" / "smoke.spec.ts",
            'const cfg = require("../config");\n',
        )
        result = has_existing_tests(tmp_path, "src/config.ts")
        assert result.proceed is False

    def test_unrelated_import_does_not_block(self, tmp_path: Path):
        _write(tmp_path / "src" / "math.ts")
        _write(tmp_path / "src" / "other.ts")
        _write(
            tmp_path / "tests" / "other.test.ts",
            'import { foo } from "../src/other";\n',
        )
        result = has_existing_tests(tmp_path, "src/math.ts")
        assert result.proceed is True


class TestHasExistingTestsJavaScript:
    def test_dot_test_js_blocks(self, tmp_path: Path):
        _write(tmp_path / "src" / "util.js")
        _write(tmp_path / "src" / "util.test.js")
        result = has_existing_tests(tmp_path, "src/util.js")
        assert result.proceed is False
        assert "util.test.js" in result.reason

    def test_jsx_with_plain_js_spec_blocks(self, tmp_path: Path):
        _write(tmp_path / "src" / "Card.jsx")
        _write(tmp_path / "src" / "Card.spec.js")
        result = has_existing_tests(tmp_path, "src/Card.jsx")
        assert result.proceed is False
        assert "Card.spec.js" in result.reason

    def test_mjs_test_naming_blocks(self, tmp_path: Path):
        _write(tmp_path / "lib" / "loader.mjs")
        _write(tmp_path / "lib" / "loader.test.mjs")
        result = has_existing_tests(tmp_path, "lib/loader.mjs")
        assert result.proceed is False


class TestHasExistingTestsUnknownLanguage:
    def test_unknown_extension_proceeds_so_stage_b_can_reject(self, tmp_path: Path):
        """A ``.go`` file should not crash Stage A — it returns ``ok``
        and Stage B handles the real rejection."""
        _write(tmp_path / "main.go", "package main\n")
        result = has_existing_tests(tmp_path, "main.go")
        assert result.proceed is True
        assert result.reason == "ok"


# ---------------------------------------------------------------------------
# Stage B — TS/JS coverage
# ---------------------------------------------------------------------------
class TestIsFeasibleTypeScriptOptIn:
    async def test_typescript_passes_when_recipe_supports_it(self, db_session):
        """The recipe doesn't yet generate TS, but the gate must accept
        TS files when the caller widens ``recipe_languages`` (preview
        does this so users can calibrate detection)."""
        repo = await _make_repo(db_session)
        await _make_file(
            db_session,
            repo,
            "src/utils.ts",
            language="typescript",
            structure={"functions": [{"name": "format"}]},
        )
        result = await is_feasible(
            db_session,
            repo.id,
            "owner/repo",
            "src/utils.ts",
            recipe_languages=frozenset({"python", "typescript"}),
        )
        assert result.proceed is True
        assert result.reason == "ok"

    async def test_d_ts_declaration_rejected(self, db_session):
        """Type-only ``.d.ts`` files have no runtime behavior — generating
        tests against them is meaningless, so reject up-front even when
        TS is in ``recipe_languages``."""
        repo = await _make_repo(db_session)
        await _make_file(
            db_session,
            repo,
            "src/types.d.ts",
            language="typescript",
            structure={"functions": [{"name": "Identifier"}]},
        )
        result = await is_feasible(
            db_session,
            repo.id,
            "owner/repo",
            "src/types.d.ts",
            recipe_languages=frozenset({"python", "typescript"}),
        )
        assert result.proceed is False
        assert "typescript_declaration" in result.reason

    async def test_typescript_main_function_passes(self, db_session):
        """Python's ``main`` + ``__name__ == '__main__'`` heuristic must
        not bleed into TS — TS has no such convention."""
        repo = await _make_repo(db_session)
        await _make_file(
            db_session,
            repo,
            "src/app.ts",
            language="typescript",
            structure={"functions": [{"name": "main"}]},
            content="function main() {}\n// has no __name__ guard\n",
        )
        result = await is_feasible(
            db_session,
            repo.id,
            "owner/repo",
            "src/app.ts",
            recipe_languages=frozenset({"python", "typescript"}),
        )
        assert result.proceed is True

    async def test_typescript_no_public_symbols_still_rejected(self, db_session):
        repo = await _make_repo(db_session)
        await _make_file(
            db_session,
            repo,
            "src/internals.ts",
            language="typescript",
            structure={"functions": [{"name": "_helper"}]},
        )
        result = await is_feasible(
            db_session,
            repo.id,
            "owner/repo",
            "src/internals.ts",
            recipe_languages=frozenset({"python", "typescript"}),
        )
        assert result.proceed is False
        assert "no_public_symbols" in result.reason


class TestIsFeasibleJavaScriptOptIn:
    async def test_javascript_passes_when_recipe_supports_it(self, db_session):
        repo = await _make_repo(db_session)
        await _make_file(
            db_session,
            repo,
            "src/app.js",
            language="javascript",
            structure={"functions": [{"name": "go"}]},
        )
        result = await is_feasible(
            db_session,
            repo.id,
            "owner/repo",
            "src/app.js",
            recipe_languages=frozenset({"python", "javascript"}),
        )
        assert result.proceed is True


# ---------------------------------------------------------------------------
# preview_repo — TS/JS surface
# ---------------------------------------------------------------------------
class TestPreviewRepoIncludesTSJS:
    async def test_preview_scans_typescript_files(self, db_session, tmp_path: Path):
        repo = await _make_repo(db_session)
        await _make_file(
            db_session,
            repo,
            "src/util.ts",
            language="typescript",
            structure={"functions": [{"name": "fmt"}]},
        )
        await _make_file(
            db_session,
            repo,
            "src/util.py",
            language="python",
            structure={"functions": [{"name": "fmt"}]},
        )

        summary = await preview_repo(
            db_session,
            repo.id,
            "owner/repo",
            tmp_path,
            default_branch="main",
            auto_test_generation=False,
        )
        scanned_paths = {f.target_file for f in summary.files}
        assert scanned_paths == {"src/util.ts", "src/util.py"}

    async def test_preview_marks_typescript_unsupported_by_default(
        self, db_session, tmp_path: Path
    ):
        """By default the recipe is Python-only — preview should surface
        TS files with a clear ``unsupported_language`` reason so users
        can see the detection but understand why nothing would fire."""
        repo = await _make_repo(db_session)
        await _make_file(
            db_session,
            repo,
            "src/util.ts",
            language="typescript",
            structure={"functions": [{"name": "fmt"}]},
        )
        summary = await preview_repo(
            db_session,
            repo.id,
            "owner/repo",
            tmp_path,
            default_branch="main",
            auto_test_generation=False,
        )
        assert len(summary.files) == 1
        ts_file = summary.files[0]
        assert ts_file.target_file == "src/util.ts"
        assert ts_file.stage_a.proceed is True
        assert ts_file.stage_b is not None
        assert ts_file.stage_b.proceed is False
        assert "unsupported_language" in ts_file.stage_b.reason


def test_supported_preview_languages_covers_recipe_set():
    """``RECIPE_SUPPORTED_LANGUAGES`` must always be a subset — preview
    should never report on a language the recipe handles unless preview
    knows how to scan for it."""
    from gita.agents.test_generator.preflight import RECIPE_SUPPORTED_LANGUAGES

    assert RECIPE_SUPPORTED_LANGUAGES <= SUPPORTED_PREVIEW_LANGUAGES


# Make the dataclass explicitly imported so importers stay typed in CI.
def test_preflight_result_is_immutable():
    r = PreflightResult(proceed=True, reason="ok")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.proceed = False  # type: ignore[misc]
