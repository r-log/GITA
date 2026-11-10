"""Pre-flight gates for the auto-test-generation trigger.

Two purely deterministic checks that run **before** any LLM call,
GitHub call, or subprocess. The post-reindex auto-trigger applies them
in order and only spends an LLM call on files that pass both:

* :func:`has_existing_tests` — Stage A. Filesystem-based detection.
  Test files are not in ``code_index`` (the indexer's
  ``include_tests=False`` default), so the source of truth is the
  repo's own working tree. Two signals: known-shape path checks +
  content grep for imports of the target's importable name.
* :func:`is_feasible` — Stage B. Indexed-state feasibility. Asks:
  given the file's structure, can we plausibly generate meaningful
  tests for it? Also dedupes against any prior auto-trigger run for
  the same target_file via ``agent_actions``.

Stage A is sync (filesystem-only). Stage B is async (DB-only).
Both return ``PreflightResult``: ``proceed=False`` is terminal —
the runner enqueues nothing and logs the ``reason``.

**Note on indexing:** test files are excluded from ``code_index`` by
default (see ``walker.py``), so Stage A looks at the filesystem. Indexing
tests (``include_tests=True``) would let Stage A become an
``ImportEdge`` query instead.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy import text as _sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from gita.db.models import CodeIndex
from gita.indexer.imports import discover_package_roots
from gita.indexer.walker import SKIP_DIRS, TEST_DIRS

logger = logging.getLogger(__name__)


# Files larger than this are skipped — long modules produce expensive
# prompts and the recipe's success rate falls off a cliff above ~500 LOC.
_MAX_LINES_FOR_TEST_GEN = 500

# Languages whose ``code_index`` rows have function/class structure that
# the test-generator's structural feasibility checks understand. Used
# as the default scan set for :func:`preview_repo`. The recipe-supported
# subset (below) is what the auto-trigger actually enqueues.
SUPPORTED_PREVIEW_LANGUAGES: frozenset[str] = frozenset({"python", "typescript", "javascript"})

# Languages the recipe can currently produce verified test files for.
# :func:`is_feasible` rejects anything outside this set with
# ``infeasible:unsupported_language:<lang>`` so the auto-trigger never
# enqueues a job the recipe can't fulfil.
RECIPE_SUPPORTED_LANGUAGES: frozenset[str] = frozenset({"python"})

# Source-file extensions per language family.
_PY_SOURCE_EXTS: tuple[str, ...] = (".py",)
_JSTS_SOURCE_EXTS: tuple[str, ...] = (
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
)

# Python test-file naming conventions — first hit on either side blocks.
_PY_TEST_PREFIXES: tuple[str, ...] = ("test_",)
_PY_TEST_SUFFIXES: tuple[str, ...] = ("_test.py",)

# JS/TS test-file naming conventions — ``foo.test.ts``, ``foo.spec.tsx``
# style. Detection lives in :func:`_is_jstS_test_filename` because the
# pattern is a middle-segment match, not a prefix/suffix.
_JSTS_TEST_SEGMENTS: tuple[str, ...] = ("test", "spec")


@dataclass(frozen=True)
class PreflightResult:
    """Outcome of a single preflight gate."""

    # ``True`` → proceed to the next gate / generation.
    # ``False`` → terminate; ``reason`` explains why.
    proceed: bool
    reason: str


# ---------------------------------------------------------------------------
# Stage A — tests-already-exist detection (filesystem)
# ---------------------------------------------------------------------------
def has_existing_tests(repo_root: Path, target_file: str) -> PreflightResult:
    """Stage A — return ``proceed=False`` when a test for ``target_file``
    already exists on disk.

    Language-aware. Sibling/mirror name shapes and the grep patterns
    differ between Python (dotted-module imports) and JS/TS (path-based
    imports). Files with extensions outside both families short-circuit
    to ``proceed=True`` — Stage B is responsible for rejecting them.

    Two layered checks; first hit wins:

    1. **Path scan.** Anywhere under ``repo_root`` that is in a
       known test directory (``tests/``, ``test/``, ``spec/``,
       ``__tests__/``) or sits next to the target, look for a file
       whose basename matches the language's test-file convention
       (``test_<stem>.py`` / ``<stem>_test.py`` for Python;
       ``<stem>.test.<ext>`` / ``<stem>.spec.<ext>`` for JS/TS).
    2. **Content grep.** If no path match, derive plausible import
       references for ``target_file`` and grep test-like files for
       them. Catches custom layouts where the test file's name
       doesn't follow the convention.

    Pure filesystem read; no DB, no LLM, no subprocess.
    """
    repo_root = repo_root.resolve()
    target_path = PurePosixPath(target_file)

    family = _classify_language(target_path.suffix)
    if family is None:
        # Unknown extension — leave the verdict to Stage B, which will
        # reject on ``not_indexed`` or ``unsupported_language``.
        return PreflightResult(proceed=True, reason="ok")

    sibling_candidates = _sibling_test_candidates(target_path)

    # ---- Check 1: known-shape path scan ----
    target_parent_abs = (repo_root / target_path).parent.resolve()

    # 1a. Sibling tests next to the target file.
    for cand_name in sibling_candidates:
        sibling = target_parent_abs / cand_name
        if sibling.is_file():
            return PreflightResult(
                proceed=False,
                reason=(f"tests_exist:sibling:{_relpath(sibling, repo_root)}"),
            )

    # 1b. Walk for the same names anywhere inside a test dir.
    for hit in _iter_repo_files(repo_root, target_only_names=tuple(sibling_candidates)):
        rel_parts = hit.relative_to(repo_root).parts
        if any(part in TEST_DIRS for part in rel_parts):
            return PreflightResult(
                proceed=False,
                reason=(f"tests_exist:in_test_dir:{_relpath(hit, repo_root)}"),
            )

    # ---- Check 2: content grep against import references ----
    patterns = _build_import_patterns_for_target(target_file, repo_root, family)
    if patterns:
        for test_file in _iter_test_like_files(repo_root, family):
            try:
                content = test_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for pat, label in patterns:
                if pat.search(content):
                    return PreflightResult(
                        proceed=False,
                        reason=(
                            f"tests_exist:imports_target:"
                            f"{_relpath(test_file, repo_root)}"
                            f" (matched `{label}`)"
                        ),
                    )

    return PreflightResult(proceed=True, reason="ok")


def _classify_language(suffix: str) -> str | None:
    """Map a file suffix (``.ts``, ``.py``, …) to a language family.

    Returns ``"python"``, ``"jstS"`` (JS/TS — same family for testing
    purposes), or ``None`` for anything else.
    """
    if suffix in _PY_SOURCE_EXTS:
        return "python"
    if suffix in _JSTS_SOURCE_EXTS:
        return "jstS"
    return None


def _sibling_test_candidates(target_path: PurePosixPath) -> list[str]:
    """Basenames a sibling test file might have for ``target_path``.

    Python:    ``test_<stem>.py``, ``<stem>_test.py``
    JS/TS:     ``<stem>.test.<ext>`` and ``<stem>.spec.<ext>`` for the
               source's own extension, plus ``.tsx`` → ``.ts`` and
               ``.jsx`` → ``.js`` fallbacks (common react-testing-library
               pattern of plain-TS tests for TSX components).
    """
    stem = target_path.stem
    ext = target_path.suffix
    if ext in _PY_SOURCE_EXTS:
        return [f"test_{stem}.py", f"{stem}_test.py"]
    if ext in _JSTS_SOURCE_EXTS:
        test_exts: list[str] = [ext]
        if ext == ".tsx":
            test_exts.append(".ts")
        elif ext == ".jsx":
            test_exts.append(".js")
        names: list[str] = []
        for segment in _JSTS_TEST_SEGMENTS:
            for test_ext in test_exts:
                names.append(f"{stem}.{segment}{test_ext}")
        return names
    return []


def _build_import_patterns_for_target(
    target_file: str, repo_root: Path, family: str
) -> list[tuple[re.Pattern[str], str]]:
    """Dispatch to the right pattern-builder for ``target_file``."""
    if family == "python":
        names = derive_importable_names(target_file, repo_root)
        return _build_import_patterns(names) if names else []
    if family == "jstS":
        return _build_jstS_import_patterns(target_file)
    return []


# ---------------------------------------------------------------------------
# Stage B — feasibility detection (indexed state)
# ---------------------------------------------------------------------------
async def is_feasible(
    session: AsyncSession,
    repo_id: uuid.UUID,
    repo_full_name: str,
    target_file: str,
    *,
    recipe_languages: frozenset[str] = RECIPE_SUPPORTED_LANGUAGES,
) -> PreflightResult:
    """Stage B — return ``proceed=False`` when the file isn't a sensible
    test-generation target, or when the auto-trigger has already
    attempted it before.

    ``recipe_languages`` is the set of indexer-language strings the
    caller will accept (the recipe currently produces verified output
    only for ``"python"``). Defaults to :data:`RECIPE_SUPPORTED_LANGUAGES`
    so the auto-trigger keeps its Python-only behavior; :func:`preview_repo`
    overrides this to surface TS/JS files in calibration output.

    Checks (all must pass):

    * file is in ``code_index`` for ``repo_id``
    * file's language is in ``recipe_languages``
    * file is not a TypeScript declaration (``*.d.ts``)
    * source ≤ ``_MAX_LINES_FOR_TEST_GEN`` lines
    * ``structure`` has at least one function or class
    * at least one symbol is public (not all underscore-prefixed)
    * (Python only) not a pure entrypoint — ``__main__.py`` or a file
      whose only public function is ``main`` plus a
      ``__name__ == "__main__"`` guard
    * no prior ``test_generator`` action recorded against this target
      in ``agent_actions``
    """
    row = (
        await session.execute(
            select(CodeIndex).where(
                CodeIndex.repo_id == repo_id,
                CodeIndex.file_path == target_file,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return PreflightResult(proceed=False, reason="infeasible:not_indexed")

    if row.language not in recipe_languages:
        return PreflightResult(
            proceed=False,
            reason=f"infeasible:unsupported_language:{row.language}",
        )

    # TypeScript declaration files have no executable code — no point
    # generating tests against type-only signatures.
    if target_file.endswith(".d.ts"):
        return PreflightResult(proceed=False, reason="infeasible:typescript_declaration")

    if row.line_count > _MAX_LINES_FOR_TEST_GEN:
        return PreflightResult(
            proceed=False,
            reason=(f"infeasible:too_large:{row.line_count} > {_MAX_LINES_FOR_TEST_GEN}"),
        )

    structure = row.structure or {}
    funcs = list(structure.get("functions", []))
    classes = list(structure.get("classes", []))
    if not funcs and not classes:
        return PreflightResult(proceed=False, reason="infeasible:no_functions_or_classes")

    public_funcs = [f for f in funcs if not f.get("name", "").startswith("_")]
    public_classes = [c for c in classes if not c.get("name", "").startswith("_")]
    if not public_funcs and not public_classes:
        return PreflightResult(proceed=False, reason="infeasible:no_public_symbols")

    # Entrypoint heuristic — Python-specific. JS/TS has no analog of
    # ``__name__ == "__main__"`` and ``index.{ts,js}`` files routinely
    # carry real testable logic, so no equivalent rejection here.
    if row.language == "python":
        file_basename = PurePosixPath(target_file).name
        if file_basename == "__main__.py":
            return PreflightResult(proceed=False, reason="infeasible:dunder_main_entrypoint")
        if (
            len(public_funcs) == 1
            and public_funcs[0].get("name") == "main"
            and not public_classes
            and '__name__ == "__main__"' in (row.content or "")
        ):
            return PreflightResult(proceed=False, reason="infeasible:cli_entrypoint")

    if await _has_prior_test_gen_attempt(session, repo_full_name, target_file):
        return PreflightResult(proceed=False, reason="infeasible:prior_attempt_exists")

    return PreflightResult(proceed=True, reason="ok")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _relpath(p: Path, root: Path) -> str:
    """Repo-relative posix path for logs."""
    try:
        return str(p.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(p)


def _iter_repo_files(root: Path, *, target_only_names: tuple[str, ...] | None = None):
    """Yield Path objects under ``root`` while skipping vendored / build dirs.

    ``target_only_names`` short-circuits the basename check before any
    further work — keeps the path scan fast on large repos.
    """
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if target_only_names and path.name not in target_only_names:
            continue
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        yield path


def _iter_test_like_files(root: Path, family: str):
    """Yield files that look like tests for the given language ``family``.

    A file qualifies if it lives under a known test directory OR its
    basename matches the language's test-naming convention. Source-file
    extensions outside the family are skipped — a Python ``has_existing_tests``
    call shouldn't grep TS source code, and vice-versa.
    """
    if family == "python":
        source_exts = _PY_SOURCE_EXTS
    elif family == "jstS":
        source_exts = _JSTS_SOURCE_EXTS
    else:
        return

    for path in _iter_repo_files(root):
        if path.suffix not in source_exts:
            continue
        rel_parts = path.relative_to(root).parts
        in_test_dir = any(part in TEST_DIRS for part in rel_parts)
        if family == "python":
            named_like_test = any(path.name.startswith(p) for p in _PY_TEST_PREFIXES) or any(
                path.name.endswith(s) for s in _PY_TEST_SUFFIXES
            )
        else:
            named_like_test = _is_jstS_test_filename(path.name)
        if in_test_dir or named_like_test:
            yield path


def _is_jstS_test_filename(name: str) -> bool:
    """``True`` for ``foo.test.ts``, ``index.spec.tsx``, etc.

    Splits on ``.`` and looks for ``test`` or ``spec`` as an interior
    segment with a JS/TS extension at the end. Avoids matching files
    like ``myapp.config.ts`` (no ``test``/``spec`` interior segment) or
    ``test.ts`` (the segment must be interior — first part stays as the
    stem).
    """
    parts = name.split(".")
    if len(parts) < 3:
        return False
    final_ext = "." + parts[-1]
    if final_ext not in _JSTS_SOURCE_EXTS:
        return False
    return any(seg in _JSTS_TEST_SEGMENTS for seg in parts[1:-1])


def _build_jstS_import_patterns(
    target_file: str,
) -> list[tuple[re.Pattern[str], str]]:
    """Patterns to detect a JS/TS test file importing the target.

    Unlike Python's dotted-module imports, JS/TS imports are path-based
    (``./foo``, ``../foo.ts``, ``@/myapp/foo``). The patterns match the
    basename of the target inside any string literal that follows
    ``from``, ``import`` (side-effect or dynamic), or ``require``.

    Conservative over-match policy: a hit means we skip generation. A
    false positive (test file importing some unrelated module that
    shares a basename) just means the user runs ``gita generate-tests``
    manually if they really want coverage — strictly safer than
    over-generating into a repo that already has the tests we'd produce.
    """
    stem = PurePosixPath(target_file).stem
    if not stem:
        return []
    esc_stem = re.escape(stem)
    # Path inside the string literal: optional path-prefix, then the
    # stem as a whole word, optionally followed by an extension. Using
    # ``[^'"]*?`` (non-greedy) so we don't accidentally span quotes.
    inner = rf"[^'\"]*?\b{esc_stem}(?:\.\w+)?"
    return [
        (
            re.compile(rf"\bfrom\s+['\"]{inner}['\"]"),
            f"from … {stem}",
        ),
        (
            re.compile(rf"\bimport\s*\(?\s*['\"]{inner}['\"]"),
            f"import … {stem}",
        ),
        (
            re.compile(rf"\brequire\s*\(\s*['\"]{inner}['\"]"),
            f"require(… {stem})",
        ),
    ]


def _build_import_patterns(
    importable_names: list[str],
) -> list[tuple[re.Pattern[str], str]]:
    """Build the regex patterns the grep tries against each test file.

    For ``myapp.core`` we accept:

    * ``import myapp.core``                 → direct import
    * ``from myapp.core import …``          → submodule attribute import
    * ``from myapp import …, core, …``      → parent-import + child name

    The third form is the common Python idiom and the easy thing for
    pure substring matching to miss. Per pattern, we also return a
    short label for the log message.
    """
    patterns: list[tuple[re.Pattern[str], str]] = []
    for name in importable_names:
        esc = re.escape(name)
        patterns.append((re.compile(rf"\bimport\s+{esc}\b"), f"import {name}"))
        patterns.append((re.compile(rf"\bfrom\s+{esc}\s+import\b"), f"from {name}"))
        if "." in name:
            parent, child = name.rsplit(".", 1)
            esc_parent = re.escape(parent)
            esc_child = re.escape(child)
            # `from <parent> import <names>` — child must appear as a
            # whole word in the import list (which extends to EOL).
            patterns.append(
                (
                    re.compile(
                        rf"\bfrom\s+{esc_parent}\s+import\s+[^\n]*"
                        rf"\b{esc_child}\b"
                    ),
                    f"from {parent} import ... {child} ...",
                )
            )
    return patterns


def derive_importable_names(target_file: str, repo_root: Path) -> list[str]:
    """Return every importable dotted-module name for ``target_file``.

    Reuses the indexer's package-root discovery so the names match what
    a real ``from <name> import ...`` line in a test file would look
    like — including ``src/`` layouts, ``backend/`` layouts, etc.
    """
    abs_target = (repo_root / target_file).resolve()
    if not abs_target.is_file():
        return []

    package_roots = discover_package_roots(repo_root)
    names: list[str] = []
    seen: set[str] = set()
    for root in package_roots:
        try:
            rel = abs_target.relative_to(root.resolve())
        except ValueError:
            continue
        # Convert "myapp/utils.py" -> ["myapp", "utils"]
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        dotted = ".".join(parts)
        if dotted not in seen:
            seen.add(dotted)
            names.append(dotted)
    return names


async def _has_prior_test_gen_attempt(
    session: AsyncSession, repo_full_name: str, target_file: str
) -> bool:
    """True if a prior ``test_generator`` action mentions ``target_file``.

    The bridge embeds ``"purpose: add tests for `<target_file>`"`` into
    the ``create_branch`` evidence array, so a JSONB ``@>`` containment
    lookup finds prior attempts. Only attempts in the current write
    mode's partition count: a shadow run (nothing reached GitHub) must not
    stop the live trigger from ever firing for that file.
    """
    from gita.config import settings

    repo_lower = repo_full_name.strip().lower()
    evidence_marker = f"purpose: add tests for `{target_file}`"
    stmt = _sql_text(
        "SELECT id FROM agent_actions "
        "WHERE LOWER(repo_name) = :repo "
        "AND agent = 'test_generator' "
        "AND live = :live "
        "AND evidence @> CAST(:evidence_json AS JSONB) "
        "LIMIT 1"
    ).bindparams(
        repo=repo_lower,
        live=settings.write_mode != "shadow",
        # json.dumps, not an f-string: paths can contain quotes/backslashes.
        evidence_json=json.dumps([evidence_marker]),
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# Preview — calibration tool for the auto-trigger
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FilePreviewResult:
    """One file's outcome through Stages A → B for the preview command."""

    target_file: str
    stage_a: PreflightResult
    # ``None`` when Stage A skipped — Stage B is never asked because A
    # already terminated. ``PreflightResult`` otherwise (proceed True or False).
    stage_b: PreflightResult | None

    @property
    def is_candidate(self) -> bool:
        return self.stage_a.proceed and bool(self.stage_b and self.stage_b.proceed)


@dataclass(frozen=True)
class PreviewSummary:
    """Aggregate output of ``preview_repo``."""

    repo_full_name: str
    default_branch: str
    auto_test_generation: bool
    total_scanned: int
    files: list[FilePreviewResult]

    @property
    def candidates(self) -> list[FilePreviewResult]:
        return [f for f in self.files if f.is_candidate]

    @property
    def rejected_by_stage_a(self) -> list[FilePreviewResult]:
        return [f for f in self.files if not f.stage_a.proceed]

    @property
    def rejected_by_stage_b(self) -> list[FilePreviewResult]:
        return [f for f in self.files if f.stage_a.proceed and f.stage_b and not f.stage_b.proceed]


async def preview_repo(
    session: AsyncSession,
    repo_id: uuid.UUID,
    repo_full_name: str,
    repo_root: Path,
    *,
    default_branch: str,
    auto_test_generation: bool,
) -> PreviewSummary:
    """Run Stages A → B over every indexed file in ``SUPPORTED_PREVIEW_LANGUAGES``.

    Pure read-only — no LLM, no GitHub, no enqueue. Used by ``gita
    auto-test-gen preview <repo>`` so the user can calibrate the gates
    against a real codebase before flipping the opt-in flag.

    The scan covers Python + TypeScript + JavaScript so users can see
    detection coverage for languages outside the recipe's current
    output set; those files surface in the report with
    ``infeasible:unsupported_language:<lang>`` until the recipe gains
    that language. Files are scanned in alphabetical order (matches
    the auto-trigger's deterministic selection order, so the candidate
    at the top is the one a real run would actually pick under
    ``cap=1``).
    """
    stmt = (
        select(CodeIndex.file_path)
        .where(CodeIndex.repo_id == repo_id)
        .where(CodeIndex.language.in_(SUPPORTED_PREVIEW_LANGUAGES))
        .order_by(CodeIndex.file_path)
    )
    paths = list((await session.execute(stmt)).scalars().all())

    files: list[FilePreviewResult] = []
    for path in paths:
        a = has_existing_tests(repo_root, path)
        b: PreflightResult | None = None
        if a.proceed:
            b = await is_feasible(session, repo_id, repo_full_name, path)
        files.append(FilePreviewResult(target_file=path, stage_a=a, stage_b=b))

    return PreviewSummary(
        repo_full_name=repo_full_name,
        default_branch=default_branch,
        auto_test_generation=auto_test_generation,
        total_scanned=len(paths),
        files=files,
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------
__all__ = [
    "RECIPE_SUPPORTED_LANGUAGES",
    "SUPPORTED_PREVIEW_LANGUAGES",
    "FilePreviewResult",
    "PreflightResult",
    "PreviewSummary",
    "has_existing_tests",
    "is_feasible",
    "preview_repo",
]
