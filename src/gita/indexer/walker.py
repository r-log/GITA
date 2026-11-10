"""File discovery + allowlist for the ingest pipeline.

``iter_files(root)`` yields ``(path, language)`` tuples for every source file
under ``root`` that survives the filter. Filter rules:

- Extension must map to a supported language (see ``LANGUAGE_BY_EXT``).
- Must NOT be in an excluded directory (``SKIP_DIRS``) — catches vendored,
  generated, and virtualenv paths.
- Must NOT match a skipped suffix (``SKIP_SUFFIXES``) — catches minified JS,
  type-only declaration files, protobuf stubs, and lockfiles.
- Must be < ``MAX_FILE_SIZE`` bytes (default 200 KB).
- Tests/specs are excluded by default (the agents analyse product code);
  pass ``include_tests=True`` to index them.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

LANGUAGE_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
}

SKIP_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".git",
        ".hg",
        ".svn",
        "dist",
        "build",
        "out",
        "target",
        ".next",
        ".nuxt",
        ".cache",
        "coverage",
        ".coverage",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "vendor",
        ".idea",
        ".vscode",
    }
)

TEST_DIRS: frozenset[str] = frozenset({"tests", "test", "spec", "__tests__"})

SKIP_SUFFIXES: tuple[str, ...] = (
    ".min.js",
    ".min.css",
    ".d.ts",
    ".pyi",
    "_pb2.py",
    "_pb2_grpc.py",
    ".lock",
)

MAX_FILE_SIZE = 200_000  # 200 KB


@dataclass(frozen=True)
class DiscoveredFile:
    path: Path
    relative_path: str  # forward-slash normalized, relative to repo root
    language: str
    size: int


def _is_skipped(path: Path, include_tests: bool) -> bool:
    parts = path.parts
    for part in parts:
        if part in SKIP_DIRS:
            return True
        if not include_tests and part in TEST_DIRS:
            return True
    return any(path.name.endswith(suffix) for suffix in SKIP_SUFFIXES)


def indexable_language(
    root: Path,
    relative_path: str,
    *,
    include_tests: bool = False,
    max_file_size: int = MAX_FILE_SIZE,
) -> str | None:
    """The file's language if it should be indexed, else ``None``.

    The single filter shared by full and incremental indexing: supported
    extension, not in a skipped directory, a regular file (symlinks are
    never followed — a committed ``x.py -> /etc/passwd`` must not end up
    in the database, the embeddings API, or a prompt), and under the size
    cap.
    """
    rel = Path(relative_path)
    language = LANGUAGE_BY_EXT.get(rel.suffix)
    if language is None or _is_skipped(rel, include_tests):
        return None
    path = root / rel
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > max_file_size:
            return None
    except OSError:
        return None
    return language


def iter_files(
    root: Path,
    *,
    include_tests: bool = False,
    max_file_size: int = MAX_FILE_SIZE,
) -> Iterator[DiscoveredFile]:
    """Yield source files under ``root`` that pass the allowlist.

    In a git work tree only tracked files are considered, so the index
    reflects the commit (which is what incremental updates diff against)
    rather than whatever untracked scratch files sit in the checkout.
    Outside git, the filesystem is walked.
    """
    from gita.indexer.diff import tracked_files

    root = root.resolve()
    tracked = tracked_files(root)
    if tracked is not None:
        relative_paths = sorted(tracked)
    else:
        relative_paths = sorted(
            str(p.relative_to(root)).replace("\\", "/")
            for p in root.rglob("*")
            if p.is_file() or p.is_symlink()
        )
    for relative in relative_paths:
        language = indexable_language(
            root, relative, include_tests=include_tests, max_file_size=max_file_size
        )
        if language is None:
            continue
        path = root / relative
        yield DiscoveredFile(
            path=path,
            relative_path=relative,
            language=language,
            size=path.stat().st_size,
        )
