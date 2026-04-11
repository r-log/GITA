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
"""
from __future__ import annotations

import re
from pathlib import Path

_PY_EXT_CANDIDATES: tuple[str, ...] = (".py",)
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
def resolve_python_import(
    raw: str, source_file: Path, repo_root: Path
) -> Path | None:
    """Try to resolve a Python import statement to a file inside ``repo_root``.

    Handles:
      - ``import foo`` / ``import foo.bar`` (absolute)
      - ``from foo.bar import baz`` (absolute)
      - ``from . import x`` (relative)
      - ``from .foo import x`` / ``from ..bar.baz import x`` (relative)

    Returns ``None`` for stdlib / installed-package imports or anything we
    can't match against a real file on disk.
    """
    text = raw.strip().rstrip(";")
    if text.startswith("from "):
        return _resolve_python_from_import(text, source_file, repo_root)
    if text.startswith("import "):
        return _resolve_python_plain_import(text, repo_root)
    return None


def _resolve_python_from_import(
    text: str, source_file: Path, repo_root: Path
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

    return _find_python_module_under(module_part, repo_root)


def _resolve_python_plain_import(text: str, repo_root: Path) -> Path | None:
    # "import foo" or "import foo.bar" or "import foo as f" or "import a, b"
    body = text[len("import "):].strip()
    # Only look at the first module in a multi-import line
    first = body.split(",", 1)[0].strip()
    first = first.split(" as ", 1)[0].strip()
    if not first:
        return None
    return _find_python_module_under(first, repo_root)


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
    raw: str, source_file: Path, repo_root: Path, language: str
) -> Path | None:
    if language == "python":
        return resolve_python_import(raw, source_file, repo_root)
    if language in ("typescript", "javascript"):
        return resolve_ts_js_import(raw, source_file, repo_root)
    return None
