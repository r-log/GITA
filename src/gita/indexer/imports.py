"""Per-language import resolution.

Each resolver takes:
    raw:        the raw import statement text as captured by the parser
    source_file: absolute path to the file containing the import
    repo_root:  absolute path to the repo root

and returns an absolute path to the resolved target file, or ``None`` if the
import points outside the repo (stdlib, npm package, installed package, etc.).

Unresolved imports are NOT an error — we still store them in ``import_edges``
with ``dst_file=NULL`` so that Week 2+ can improve resolution without a
reindex.

**Package-root discovery (Week 2 P1 fix):** real Python projects rarely put
their package at the repo root. ``src/``-layouts, ``backend/``-layouts, and
deeply nested apps are normal. ``discover_package_roots`` walks the
``__init__.py`` chain to find every "topmost package directory" and treats
their parents as valid starting points for absolute-import resolution.
"""
from __future__ import annotations

import re
from pathlib import Path

from gita.indexer.walker import SKIP_DIRS, TEST_DIRS

_PACKAGE_EXCLUDE_DIRS: frozenset[str] = SKIP_DIRS | TEST_DIRS

_TSJS_EXT_CANDIDATES: tuple[str, ...] = (
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
)
_TSJS_INDEX_NAMES: tuple[str, ...] = (
    "index.ts",
    "index.tsx",
    "index.js",
    "index.jsx",
    "index.mjs",
    "index.cjs",
)

# Matches the first string literal in an import statement: supports single and
# double quotes and anything that isn't the quote char between them.
_TSJS_SPEC_RE = re.compile(r"""['"]([^'"]+)['"]""")


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------
def discover_package_roots(repo_root: Path) -> list[Path]:
    """Return every directory that can serve as a Python package-import root.

    Walks every ``__init__.py`` under ``repo_root``, follows the chain up to
    the topmost ``__init__.py``-bearing directory (that's the *package*), and
    records its parent as a *package root* — a directory against which
    absolute imports like ``from app.models import User`` can resolve.

    The repo root itself is always appended as a last-resort fallback so
    flat layouts keep working. Order: discovered roots first (deepest/most
    specific first isn't guaranteed, but each is disjoint in practice),
    repo root last.
    """
    repo_root = repo_root.resolve()
    roots: list[Path] = []
    seen: set[Path] = set()

    for init_path in repo_root.rglob("__init__.py"):
        # Only exclude based on path components INSIDE the repo — otherwise
        # a fixture living under tests/fixtures/foo/ gets all its __init__.py
        # files skipped just because "tests" appears in the absolute path.
        try:
            rel_parts = init_path.relative_to(repo_root).parts
        except ValueError:
            continue
        if any(part in _PACKAGE_EXCLUDE_DIRS for part in rel_parts):
            continue

        # Walk up while each parent also contains __init__.py — we want the
        # topmost package directory, whose PARENT is the package root.
        pkg_dir = init_path.parent
        while (pkg_dir.parent / "__init__.py").is_file():
            pkg_dir = pkg_dir.parent

        root = pkg_dir.parent
        if root == repo_root or root in seen:
            continue
        seen.add(root)
        roots.append(root)

    roots.append(repo_root)
    return roots


def resolve_python_import(
    raw: str,
    source_file: Path,
    repo_root: Path,
    package_roots: list[Path] | None = None,
) -> Path | None:
    """Try to resolve a Python import statement to a file inside ``repo_root``.

    Handles:
      - ``import foo`` / ``import foo.bar`` (absolute)
      - ``from foo.bar import baz`` (absolute)
      - ``from . import x`` (relative)
      - ``from .foo import x`` / ``from ..bar.baz import x`` (relative)

    ``package_roots`` is the list of directories to try for absolute imports.
    If omitted, ``[repo_root]`` is used (backward-compatible with callers
    that haven't been updated). ``ingest.index_repository`` computes this
    once per repo via :func:`discover_package_roots` and passes it in.

    Returns ``None`` for stdlib / installed-package imports or anything we
    can't match against a real file on disk.
    """
    if package_roots is None:
        package_roots = [repo_root]

    text = raw.strip().rstrip(";")
    if text.startswith("from "):
        return _resolve_python_from_import(text, source_file, package_roots)
    if text.startswith("import "):
        return _resolve_python_plain_import(text, package_roots)
    return None


def _resolve_python_from_import(
    text: str,
    source_file: Path,
    package_roots: list[Path],
) -> Path | None:
    # "from MODULE import NAMES..." — module is before " import "
    body = text[len("from "):]
    if " import " not in body:
        return None
    module_part = body.split(" import ", 1)[0].strip()

    # Relative: leading dots
    if module_part.startswith("."):
        dots = 0
        while dots < len(module_part) and module_part[dots] == ".":
            dots += 1
        remainder = module_part[dots:]
        # One dot = same package (source file's directory)
        # Two dots = parent, etc.
        base = source_file.parent
        for _ in range(dots - 1):
            base = base.parent
        return _find_python_module_under(remainder, base)

    return _find_python_module_in_roots(module_part, package_roots)


def _resolve_python_plain_import(
    text: str, package_roots: list[Path]
) -> Path | None:
    # "import foo" or "import foo.bar" or "import foo as f" or "import a, b"
    body = text[len("import "):].strip()
    # Only look at the first module in a multi-import line
    first = body.split(",", 1)[0].strip()
    first = first.split(" as ", 1)[0].strip()
    if not first:
        return None
    return _find_python_module_in_roots(first, package_roots)


def _find_python_module_in_roots(
    module: str, package_roots: list[Path]
) -> Path | None:
    """Try resolving a dotted module name against each package root in order."""
    for root in package_roots:
        found = _find_python_module_under(module, root)
        if found is not None:
            return found
    return None


def _find_python_module_under(module: str, base: Path) -> Path | None:
    """Given a dotted module path like ``foo.bar``, try ``base/foo/bar.py`` or
    ``base/foo/bar/__init__.py``.

    Empty ``module`` (e.g. ``from . import x``) resolves to ``base/__init__.py``.
    """
    if not module:
        candidate = base / "__init__.py"
        return candidate if candidate.is_file() else None

    parts = module.split(".")
    target = base
    for part in parts[:-1]:
        target = target / part
    last = parts[-1]

    direct = target / f"{last}.py"
    if direct.is_file():
        return direct
    as_package = target / last / "__init__.py"
    if as_package.is_file():
        return as_package
    return None


# ---------------------------------------------------------------------------
# TypeScript / JavaScript
# ---------------------------------------------------------------------------
def resolve_ts_js_import(
    raw: str, source_file: Path, repo_root: Path
) -> Path | None:
    """Try to resolve a TS/JS import statement to a file inside ``repo_root``.

    Handles:
      - ``import x from './foo'`` (relative, extension inferred)
      - ``import x from '../foo/bar'``
      - ``import x from './foo'`` where ``foo/index.ts`` exists
      - ``import x from './foo.ts'`` (explicit extension)

    Returns ``None`` for bare imports (``import x from 'react'``) — those are
    node_modules, not repo files.
    """
    match = _TSJS_SPEC_RE.search(raw)
    if match is None:
        return None
    spec = match.group(1)
    if not spec.startswith(".") and not spec.startswith("/"):
        return None  # bare import — npm package

    base = source_file.parent
    target = (base / spec).resolve()

    # Bail if the import escapes the repo
    try:
        target.relative_to(repo_root.resolve())
    except ValueError:
        return None

    # 1. Exact path (in case the import already has an extension)
    if target.is_file():
        return target

    # 2. Try appending each candidate extension
    for ext in _TSJS_EXT_CANDIDATES:
        candidate = Path(str(target) + ext)
        if candidate.is_file():
            return candidate

    # 3. Try as a directory with an index file
    if target.is_dir():
        for index_name in _TSJS_INDEX_NAMES:
            candidate = target / index_name
            if candidate.is_file():
                return candidate

    return None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
def resolve_import(
    raw: str,
    source_file: Path,
    repo_root: Path,
    language: str,
    package_roots: list[Path] | None = None,
) -> Path | None:
    if language == "python":
        return resolve_python_import(
            raw, source_file, repo_root, package_roots
        )
    if language in ("typescript", "javascript"):
        return resolve_ts_js_import(raw, source_file, repo_root)
    return None
