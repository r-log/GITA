"""Pre-flight gates for the auto-test-generation trigger (Week 9).

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

**Note on indexing:** test files are deliberately excluded from
``code_index`` (Week 1 decision in ``walker.py``). A future Week 10+
could flip ``include_tests=True`` and replace Stage A's filesystem
walk with an ``ImportEdge`` query — this is recorded in
NOTES-week9.md as Option B.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sqlalchemy import select, text as _sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from gita.db.models import CodeIndex
from gita.indexer.imports import discover_package_roots
from gita.indexer.walker import SKIP_DIRS, TEST_DIRS

logger = logging.getLogger(__name__)


# Files larger than this are skipped — long modules produce expensive
# prompts and the recipe's success rate falls off a cliff above ~500 LOC.
_MAX_LINES_FOR_TEST_GEN = 500

# Test-file naming patterns recognized by both gates.
_TEST_FILE_PREFIXES = ("test_",)
_TEST_FILE_SUFFIXES = ("_test.py",)


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
def has_existing_tests(
    repo_root: Path, target_file: str
) -> PreflightResult:
    """Stage A — return ``proceed=False`` when a test for ``target_file``
    already exists on disk.

    Two layered checks; first hit wins:

    1. **Path scan.** Anywhere under ``repo_root`` that is in a
       known test directory (``tests/``, ``test/``, ``spec/``,
       ``__tests__/``) or sits next to the target, look for a file
       named ``test_<stem>.py`` or ``<stem>_test.py``.
    2. **Content grep.** If no path match, derive every plausible
       importable module name for ``target_file`` (using the same
       package-root discovery the indexer uses) and grep test files
       for ``from <name>`` / ``import <name>``. Catches custom layouts
       where the test file's name doesn't follow the convention.

    Pure filesystem read; no DB, no LLM, no subprocess.
    """
    repo_root = repo_root.resolve()
    target_path = PurePosixPath(target_file)
    stem = target_path.stem

    # ---- Check 1: known-shape path scan ----
    target_parent_abs = (repo_root / target_path).parent.resolve()

    # 1a. Sibling tests next to the target file (Python + Go-style).
    for cand_name in (f"test_{stem}.py", f"{stem}_test.py"):
        sibling = target_parent_abs / cand_name
        if sibling.is_file():
            return PreflightResult(
                proceed=False,
                reason=(
                    f"tests_exist:sibling:"
                    f"{_relpath(sibling, repo_root)}"
                ),
            )

    # 1b. Walk for the same names anywhere inside a test dir.
    for hit in _iter_repo_files(repo_root, target_only_names=(
        f"test_{stem}.py",
        f"{stem}_test.py",
    )):
        rel_parts = hit.relative_to(repo_root).parts
        if any(part in TEST_DIRS for part in rel_parts):
            return PreflightResult(
                proceed=False,
                reason=(
                    f"tests_exist:in_test_dir:{_relpath(hit, repo_root)}"
                ),
            )

    # ---- Check 2: content grep against importable module names ----
    importable_names = _derive_importable_names(target_file, repo_root)
    if importable_names:
        patterns = _build_import_patterns(importable_names)
        for test_file in _iter_test_like_files(repo_root):
            try:
                content = test_file.read_text(
                    encoding="utf-8", errors="replace"
                )
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


# ---------------------------------------------------------------------------
# Stage B — feasibility detection (indexed state)
# ---------------------------------------------------------------------------
async def is_feasible(
    session: AsyncSession,
    repo_id: uuid.UUID,
    repo_full_name: str,
    target_file: str,
) -> PreflightResult:
    """Stage B — return ``proceed=False`` when the file isn't a sensible
    test-generation target, or when the auto-trigger has already
    attempted it before.

    Checks (all must pass):

    * file is in ``code_index`` for ``repo_id``
    * file is Python (recipe is Python-only)
    * source ≤ ``_MAX_LINES_FOR_TEST_GEN`` lines
    * ``structure`` has at least one function or class
    * at least one symbol is public (not all underscore-prefixed)
    * not a pure entrypoint (heuristic: only public name is ``main``
      AND module ends in ``__main__.py`` / has a ``__name__ == "__main__"``
      sentinel)
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
        return PreflightResult(
            proceed=False, reason="infeasible:not_indexed"
        )

    if row.language != "python":
        return PreflightResult(
            proceed=False,
            reason=f"infeasible:non_python_language:{row.language}",
        )

    if row.line_count > _MAX_LINES_FOR_TEST_GEN:
        return PreflightResult(
            proceed=False,
            reason=(
                f"infeasible:too_large:{row.line_count} > "
                f"{_MAX_LINES_FOR_TEST_GEN}"
            ),
        )

    structure = row.structure or {}
    funcs = list(structure.get("functions", []))
    classes = list(structure.get("classes", []))
    if not funcs and not classes:
        return PreflightResult(
            proceed=False, reason="infeasible:no_functions_or_classes"
        )

    public_funcs = [f for f in funcs if not f.get("name", "").startswith("_")]
    public_classes = [
        c for c in classes if not c.get("name", "").startswith("_")
    ]
    if not public_funcs and not public_classes:
        return PreflightResult(
            proceed=False, reason="infeasible:no_public_symbols"
        )

    # Entrypoint heuristic: ``__main__.py`` files, or files whose only
    # public function is named ``main`` and whose body contains a
    # ``__name__ == "__main__"`` guard. Generated tests for entrypoints
    # rarely tell us anything useful.
    file_basename = PurePosixPath(target_file).name
    if file_basename == "__main__.py":
        return PreflightResult(
            proceed=False, reason="infeasible:dunder_main_entrypoint"
        )
    if (
        len(public_funcs) == 1
        and public_funcs[0].get("name") == "main"
        and not public_classes
        and '__name__ == "__main__"' in (row.content or "")
    ):
        return PreflightResult(
            proceed=False, reason="infeasible:cli_entrypoint"
        )

    if await _has_prior_test_gen_attempt(
        session, repo_full_name, target_file
    ):
        return PreflightResult(
            proceed=False, reason="infeasible:prior_attempt_exists"
        )

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


def _iter_repo_files(
    root: Path, *, target_only_names: tuple[str, ...] | None = None
):
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


def _iter_test_like_files(root: Path):
    """Yield .py files that look like test files (in test dir OR named like one)."""
    for path in _iter_repo_files(root):
        if path.suffix != ".py":
            continue
        rel_parts = path.relative_to(root).parts
        in_test_dir = any(part in TEST_DIRS for part in rel_parts)
        named_like_test = (
            any(path.name.startswith(p) for p in _TEST_FILE_PREFIXES)
            or any(path.name.endswith(s) for s in _TEST_FILE_SUFFIXES)
        )
        if in_test_dir or named_like_test:
            yield path


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
        patterns.append(
            (re.compile(rf"\bimport\s+{esc}\b"), f"import {name}")
        )
        patterns.append(
            (re.compile(rf"\bfrom\s+{esc}\s+import\b"), f"from {name}")
        )
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


def _derive_importable_names(
    target_file: str, repo_root: Path
) -> list[str]:
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
    """True if any prior ``test_generator`` action mentions ``target_file``.

    The bridge embeds ``"purpose: add tests for `<target_file>`"`` into
    the ``create_branch`` evidence array (and some other actions), so
    an exact JSONB ``@>`` containment lookup against that string is a
    cheap, deterministic way to detect prior attempts — executed,
    downgraded, or anything else that wrote a row.

    Lookup is case-insensitive on ``repo_name`` to match how the rest
    of the code stores it (lowered when written by webhook flows).
    """
    repo_lower = repo_full_name.strip().lower()
    evidence_marker = f"purpose: add tests for `{target_file}`"
    stmt = _sql_text(
        "SELECT id FROM agent_actions "
        "WHERE LOWER(repo_name) = :repo "
        "AND agent = 'test_generator' "
        "AND evidence @> CAST(:evidence_json AS JSONB) "
        "LIMIT 1"
    ).bindparams(
        repo=repo_lower,
        evidence_json=f'["{evidence_marker}"]',
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------
__all__ = [
    "PreflightResult",
    "has_existing_tests",
    "is_feasible",
]
