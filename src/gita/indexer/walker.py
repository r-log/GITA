"""File discovery + allowlist for the Week 1 ingest pipeline.

``iter_files(root)`` yields ``(path, language)`` tuples for every source file
under ``root`` that survives the filter. Filter rules:

- Extension must map to a supported language (see ``LANGUAGE_BY_EXT``).
- Must NOT be in an excluded directory (``SKIP_DIRS``) — catches vendored,
  generated, and virtualenv paths.
- Must NOT match a skipped suffix (``SKIP_SUFFIXES``) — catches minified JS,
  type-only declaration files, protobuf stubs, and lockfiles.
- Must be < ``MAX_FILE_SIZE`` bytes (default 200 KB).
- Tests/specs are excluded by default in Week 1 — that's a Week 1 decision per
  the plan; flip ``include_tests`` if you need them.
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
    for suffix in SKIP_SUFFIXES:
        if path.name.endswith(suffix):
            return True
    return False


def iter_files(
    root: Path,
    *,
    include_tests: bool = False,
    max_file_size: int = MAX_FILE_SIZE,
) -> Iterator[DiscoveredFile]:
    """Yield source files under ``root`` that pass the allowlist."""
    root = root.resolve()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        language = LANGUAGE_BY_EXT.get(path.suffix)
        if language is None:
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if _is_skipped(relative, include_tests):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > max_file_size:
            continue
        yield DiscoveredFile(
            path=path,
            relative_path=str(relative).replace("\\", "/"),
            language=language,
            size=size,
        )
