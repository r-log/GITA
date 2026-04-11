"""
Granular code retrieval tools — let agents pull specific slices of source files
from the `code_index` table instead of re-fetching from GitHub.

Design:
  - `get_function_code(file, function_name)` → just the function's lines
  - `get_class_code(file, class_name)` → just the class body
  - `get_code_slice(file, start, end)` → explicit line range (cap 300 lines)
  - `read_file(file)` → full contents with line numbers (cap 600 lines)
  - `search_in_file(file, regex)` → matching lines + snippets
  - `list_project_files(pattern)` → file paths + languages + line counts

Every returned code slice is prefixed with 1-indexed line numbers so the LLM
can cite findings unambiguously.

All reads are served from `code_index.content`. If `content IS NULL` for a
file (unsupported language or size-capped), callers get a helpful message
hinting at `get_code_slice` or re-indexing.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any

import structlog
from sqlalchemy import select

from src.core.database import async_session
from src.models.code_index import CodeIndex
from src.tools.base import Tool, ToolResult

log = structlog.get_logger()


_MAX_SLICE_LINES = 300
_MAX_READ_FILE_LINES = 600
_MAX_SEARCH_MATCHES = 20
_MAX_LIST_FILES = 500


# ── Helpers ───────────────────────────────────────────────────────


async def _load_record(repo_id: int, file_path: str) -> CodeIndex | None:
    async with async_session() as session:
        result = await session.execute(
            select(CodeIndex).where(
                CodeIndex.repo_id == repo_id,
                CodeIndex.file_path == file_path,
            )
        )
        return result.scalar_one_or_none()


def _format_with_line_numbers(content: str, start_line: int = 1) -> str:
    """Prefix each line with `N: ` where N is the 1-indexed line number."""
    lines = content.split("\n")
    width = len(str(start_line + len(lines) - 1))
    return "\n".join(
        f"{str(start_line + i).rjust(width)}: {line}"
        for i, line in enumerate(lines)
    )


def _slice_content(content: str, start: int, end: int) -> str:
    """1-indexed inclusive slice of content by line number."""
    all_lines = content.split("\n")
    # Clamp to valid range
    start = max(1, start)
    end = min(len(all_lines), end)
    if start > end:
        return ""
    slice_lines = all_lines[start - 1:end]
    return "\n".join(slice_lines)


def _find_symbol_in_structure(
    structure: dict,
    symbol_name: str,
    symbol_kind: str,
) -> dict[str, Any] | None:
    """
    Locate a function/class/method in a structure dict.

    symbol_kind: 'function' or 'class'
    symbol_name: for methods, use 'ClassName.method_name'
    """
    if symbol_kind == "function":
        # Try top-level functions first
        for fn in structure.get("functions", []) or []:
            if fn.get("name") == symbol_name:
                return fn
        # Also try routes (they're functions with route decorators)
        for route in structure.get("routes", []) or []:
            if route.get("handler") == symbol_name:
                return route
        # Handle dotted method form: ClassName.method_name
        if "." in symbol_name:
            class_name, method_name = symbol_name.split(".", 1)
            for cls in structure.get("classes", []) or []:
                if cls.get("name") != class_name:
                    continue
                for m in cls.get("method_details", []) or []:
                    if m.get("name") == method_name:
                        return m
        return None

    if symbol_kind == "class":
        for cls in structure.get("classes", []) or []:
            if cls.get("name") == symbol_name:
                return cls

    return None


# ── get_function_code ─────────────────────────────────────────────


async def _get_function_code(
    repo_id: int, file_path: str, function_name: str
) -> ToolResult:
    record = await _load_record(repo_id, file_path)
    if not record:
        return ToolResult(success=False, error=f"file not indexed: {file_path}")
    if not record.content:
        return ToolResult(
            success=False,
            error=(
                f"file has no stored content (language={record.language}, "
                "likely unsupported or filtered out at index time). "
                "Use get_code_slice with explicit line numbers instead."
            ),
        )

    entry = _find_symbol_in_structure(record.structure or {}, function_name, "function")
    if not entry:
        return ToolResult(
            success=False,
            error=f"function '{function_name}' not found in {file_path}",
        )
    start = entry.get("line")
    end = entry.get("end_line")
    if not start:
        return ToolResult(
            success=False,
            error=f"function '{function_name}' has no line info — try get_code_slice",
        )
    if not end:
        # Parser didn't provide end_line; show 40 lines as a reasonable default
        end = start + 40

    code = _slice_content(record.content, start, end)
    numbered = _format_with_line_numbers(code, start_line=start)

    return ToolResult(
        success=True,
        data={
            "file_path": file_path,
            "function_name": function_name,
            "start_line": start,
            "end_line": end,
            "code": numbered,
        },
    )


def make_get_function_code(repo_id: int) -> Tool:
    return Tool(
        name="get_function_code",
        description=(
            "Return the source code of a specific function in a file, including "
            "line numbers. Prefer this over read_file when you only care about "
            "one function. For methods, use 'ClassName.method_name'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "File path from the code map"},
                "function_name": {
                    "type": "string",
                    "description": "Function name, or 'ClassName.method_name' for methods",
                },
            },
            "required": ["file_path", "function_name"],
        },
        handler=lambda file_path, function_name: _get_function_code(
            repo_id, file_path, function_name
        ),
    )


# ── get_class_code ────────────────────────────────────────────────


async def _get_class_code(
    repo_id: int, file_path: str, class_name: str
) -> ToolResult:
    record = await _load_record(repo_id, file_path)
    if not record:
        return ToolResult(success=False, error=f"file not indexed: {file_path}")
    if not record.content:
        return ToolResult(
            success=False,
            error=(
                f"file has no stored content (language={record.language}). "
                "Use get_code_slice with explicit line numbers instead."
            ),
        )

    entry = _find_symbol_in_structure(record.structure or {}, class_name, "class")
    if not entry:
        return ToolResult(
            success=False,
            error=f"class '{class_name}' not found in {file_path}",
        )
    start = entry.get("line")
    end = entry.get("end_line")
    if not start:
        return ToolResult(
            success=False,
            error=f"class '{class_name}' has no line info — try get_code_slice",
        )
    if not end:
        end = min(record.line_count or start + 100, start + 200)

    code = _slice_content(record.content, start, end)
    numbered = _format_with_line_numbers(code, start_line=start)

    return ToolResult(
        success=True,
        data={
            "file_path": file_path,
            "class_name": class_name,
            "start_line": start,
            "end_line": end,
            "code": numbered,
        },
    )


def make_get_class_code(repo_id: int) -> Tool:
    return Tool(
        name="get_class_code",
        description=(
            "Return the full source of a class in a file (including all its "
            "methods), with line numbers. Prefer this over read_file when you "
            "only care about one class."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "class_name": {"type": "string"},
            },
            "required": ["file_path", "class_name"],
        },
        handler=lambda file_path, class_name: _get_class_code(
            repo_id, file_path, class_name
        ),
    )


# ── get_code_slice ────────────────────────────────────────────────


async def _get_code_slice(
    repo_id: int, file_path: str, start_line: int, end_line: int
) -> ToolResult:
    record = await _load_record(repo_id, file_path)
    if not record:
        return ToolResult(success=False, error=f"file not indexed: {file_path}")
    if not record.content:
        return ToolResult(
            success=False,
            error=f"file has no stored content (language={record.language})",
        )

    if start_line < 1:
        start_line = 1
    if end_line > (record.line_count or 10_000_000):
        end_line = record.line_count or end_line
    if end_line - start_line + 1 > _MAX_SLICE_LINES:
        return ToolResult(
            success=False,
            error=(
                f"slice too large ({end_line - start_line + 1} lines). "
                f"Max {_MAX_SLICE_LINES} lines per call — narrow the range."
            ),
        )

    code = _slice_content(record.content, start_line, end_line)
    numbered = _format_with_line_numbers(code, start_line=start_line)

    return ToolResult(
        success=True,
        data={
            "file_path": file_path,
            "start_line": start_line,
            "end_line": end_line,
            "code": numbered,
        },
    )


def make_get_code_slice(repo_id: int) -> Tool:
    return Tool(
        name="get_code_slice",
        description=(
            "Return an explicit line-range slice of a file (1-indexed, inclusive) "
            "with line numbers. Use this when you know the exact lines you want. "
            f"Max {_MAX_SLICE_LINES} lines per call."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "start_line": {"type": "integer", "description": "1-indexed starting line"},
                "end_line": {"type": "integer", "description": "1-indexed inclusive end line"},
            },
            "required": ["file_path", "start_line", "end_line"],
        },
        handler=lambda file_path, start_line, end_line: _get_code_slice(
            repo_id, file_path, start_line, end_line
        ),
    )


# ── read_file ─────────────────────────────────────────────────────


async def _read_file(repo_id: int, file_path: str) -> ToolResult:
    record = await _load_record(repo_id, file_path)
    if not record:
        return ToolResult(success=False, error=f"file not indexed: {file_path}")
    if not record.content:
        return ToolResult(
            success=False,
            error=f"file has no stored content (language={record.language})",
        )

    total_lines = record.line_count or record.content.count("\n") + 1
    if total_lines > _MAX_READ_FILE_LINES:
        return ToolResult(
            success=False,
            error=(
                f"file has {total_lines} lines — too large for read_file "
                f"(max {_MAX_READ_FILE_LINES}). Use get_code_slice with an "
                f"explicit range, or get_function_code / get_class_code for "
                f"a specific symbol."
            ),
        )

    numbered = _format_with_line_numbers(record.content, start_line=1)
    return ToolResult(
        success=True,
        data={
            "file_path": file_path,
            "language": record.language,
            "line_count": total_lines,
            "code": numbered,
        },
    )


def make_read_file(repo_id: int) -> Tool:
    return Tool(
        name="read_file",
        description=(
            "Return the full contents of a file with line numbers. DISCOURAGED — "
            "prefer get_function_code, get_class_code, or get_code_slice so you "
            f"pull only what you need. Capped at {_MAX_READ_FILE_LINES} lines."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
            },
            "required": ["file_path"],
        },
        handler=lambda file_path: _read_file(repo_id, file_path),
    )


# ── search_in_file ────────────────────────────────────────────────


async def _search_in_file(
    repo_id: int, file_path: str, pattern: str
) -> ToolResult:
    record = await _load_record(repo_id, file_path)
    if not record:
        return ToolResult(success=False, error=f"file not indexed: {file_path}")
    if not record.content:
        return ToolResult(
            success=False,
            error=f"file has no stored content (language={record.language})",
        )

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return ToolResult(success=False, error=f"invalid regex: {e}")

    matches: list[dict] = []
    for i, line in enumerate(record.content.split("\n"), start=1):
        if regex.search(line):
            matches.append({"line": i, "text": line.rstrip()[:300]})
            if len(matches) >= _MAX_SEARCH_MATCHES:
                break

    return ToolResult(
        success=True,
        data={
            "file_path": file_path,
            "pattern": pattern,
            "match_count": len(matches),
            "matches": matches,
            "truncated": len(matches) >= _MAX_SEARCH_MATCHES,
        },
    )


def make_search_in_file(repo_id: int) -> Tool:
    return Tool(
        name="search_in_file",
        description=(
            "Find lines matching a regex in a specific file. Returns up to "
            f"{_MAX_SEARCH_MATCHES} matches with line numbers. Useful for "
            "locating specific patterns (e.g. 'except:\\s*$' for bare except)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "pattern": {"type": "string", "description": "Python regex pattern"},
            },
            "required": ["file_path", "pattern"],
        },
        handler=lambda file_path, pattern: _search_in_file(
            repo_id, file_path, pattern
        ),
    )


# ── list_project_files ────────────────────────────────────────────


async def _list_project_files(
    repo_id: int, pattern: str | None = None
) -> ToolResult:
    async with async_session() as session:
        result = await session.execute(
            select(
                CodeIndex.file_path,
                CodeIndex.language,
                CodeIndex.line_count,
            ).where(CodeIndex.repo_id == repo_id)
        )
        rows = result.all()

    files = [
        {"file_path": p, "language": lang, "line_count": lc}
        for (p, lang, lc) in rows
    ]

    if pattern:
        files = [f for f in files if fnmatch.fnmatch(f["file_path"], pattern)]

    # Sort deterministically so the LLM sees a stable view
    files.sort(key=lambda f: f["file_path"])

    truncated = False
    if len(files) > _MAX_LIST_FILES:
        files = files[:_MAX_LIST_FILES]
        truncated = True

    return ToolResult(
        success=True,
        data={
            "count": len(files),
            "truncated": truncated,
            "files": files,
        },
    )


def make_list_project_files(repo_id: int) -> Tool:
    return Tool(
        name="list_project_files",
        description=(
            "List files that have been indexed for this repository. Optional "
            "glob pattern filter (e.g. 'src/**/*.py'). Returns path, language, "
            f"and line count. Max {_MAX_LIST_FILES} results."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Optional glob pattern (e.g. 'src/**/*.py')",
                },
            },
        },
        handler=lambda pattern=None: _list_project_files(repo_id, pattern),
    )
