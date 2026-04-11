"""File parsing — produces a FileStructure from source code.

Each supported language has an extractor that walks a Tree-sitter parse tree
and emits functions, classes, and imports with accurate 1-indexed start/end
line numbers. The public entry point is ``parse_file``.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from tree_sitter import Query, QueryCursor

from gita.indexer.ts_loader import get_language, load_parser

logger = logging.getLogger(__name__)

QUERIES_DIR = Path(__file__).parent / "queries"


@dataclass
class Symbol:
    name: str
    kind: str  # "function" | "method" | "async_function" | "async_method" | "class"
    start_line: int  # 1-indexed
    end_line: int    # 1-indexed, inclusive
    parent_class: str | None = None


@dataclass
class ImportStmt:
    raw: str
    start_line: int


@dataclass
class FileStructure:
    functions: list[Symbol] = field(default_factory=list)
    classes: list[Symbol] = field(default_factory=list)
    imports: list[ImportStmt] = field(default_factory=list)

    def to_jsonb(self) -> dict:
        return {
            "functions": [asdict(s) for s in self.functions],
            "classes": [asdict(s) for s in self.classes],
            "imports": [asdict(i) for i in self.imports],
        }


_QUERY_CACHE: dict[str, Query] = {}


def _load_query(language: str) -> Query | None:
    if language in _QUERY_CACHE:
        return _QUERY_CACHE[language]
    lang = get_language(language)
    if lang is None:
        return None
    query_path = QUERIES_DIR / f"{language}.scm"
    if not query_path.exists():
        logger.warning("missing_query_file language=%s path=%s", language, query_path)
        return None
    query_src = query_path.read_text(encoding="utf-8")
    query = Query(lang, query_src)
    _QUERY_CACHE[language] = query
    return query


def _node_text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _nodes_from_captures(captures, name: str) -> list:
    """Compatibility shim for tree-sitter's captures() API.

    In tree-sitter>=0.22, ``query.captures()`` returns a dict
    ``{name: [nodes]}``. Older versions return ``[(node, name), ...]``.
    Both are supported.
    """
    if isinstance(captures, dict):
        return list(captures.get(name, []))
    return [node for node, cap_name in captures if cap_name == name]


def _find_enclosing_class_name(node, source: bytes) -> str | None:
    current = node.parent
    while current is not None:
        if current.type == "class_definition":
            name_node = current.child_by_field_name("name")
            if name_node is not None:
                return _node_text(name_node, source)
            return None
        current = current.parent
    return None


def _is_async_function(node) -> bool:
    for child in node.children:
        if child.type == "async":
            return True
    return False


def _extract_python(source: bytes, tree) -> FileStructure:
    query = _load_query("python")
    if query is None:
        return FileStructure()

    structure = FileStructure()
    captures = QueryCursor(query).captures(tree.root_node)

    for func_node in _nodes_from_captures(captures, "function.body"):
        name_node = func_node.child_by_field_name("name")
        if name_node is None:
            continue
        name = _node_text(name_node, source)
        parent_class = _find_enclosing_class_name(func_node, source)
        is_async = _is_async_function(func_node)
        if parent_class is not None:
            kind = "async_method" if is_async else "method"
        else:
            kind = "async_function" if is_async else "function"
        structure.functions.append(
            Symbol(
                name=name,
                kind=kind,
                start_line=func_node.start_point[0] + 1,
                end_line=func_node.end_point[0] + 1,
                parent_class=parent_class,
            )
        )

    for class_node in _nodes_from_captures(captures, "class.body"):
        name_node = class_node.child_by_field_name("name")
        if name_node is None:
            continue
        structure.classes.append(
            Symbol(
                name=_node_text(name_node, source),
                kind="class",
                start_line=class_node.start_point[0] + 1,
                end_line=class_node.end_point[0] + 1,
            )
        )

    for imp_node in _nodes_from_captures(captures, "import"):
        structure.imports.append(
            ImportStmt(
                raw=_node_text(imp_node, source),
                start_line=imp_node.start_point[0] + 1,
            )
        )

    return structure


_EXTRACTORS = {
    "python": _extract_python,
}


def parse_file(path: Path, content: str, language: str) -> FileStructure:
    """Parse a source file and return its structure.

    Returns an empty FileStructure on any failure (unsupported language,
    Tree-sitter load failure, parse error). Never raises.
    """
    parser = load_parser(language)
    if parser is None:
        return FileStructure()
    extractor = _EXTRACTORS.get(language)
    if extractor is None:
        return FileStructure()
    source = content.encode("utf-8")
    try:
        tree = parser.parse(source)
    except Exception as exc:
        logger.warning("parse_failed path=%s language=%s error=%s", path, language, exc)
        return FileStructure()
    return extractor(source, tree)
