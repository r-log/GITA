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


_DOCSTRING_CAP = 200


@dataclass
class Symbol:
    name: str
    kind: str  # "function" | "method" | "async_function" | "async_method" | "class"
    start_line: int  # 1-indexed
    end_line: int    # 1-indexed, inclusive
    parent_class: str | None = None
    signature: str | None = None    # e.g. "def foo(x: int, y: str) -> bool"
    docstring: str | None = None    # first line, capped at _DOCSTRING_CAP chars
    decorators: list[str] = field(default_factory=list)  # e.g. ["@property"]


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


def _find_enclosing_class_name(
    node, source: bytes, class_node_type: str
) -> str | None:
    """Walk up the AST to find the nearest enclosing class node and return its name."""
    current = node.parent
    while current is not None:
        if current.type == class_node_type:
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


def _extract_python_signature(func_node, source: bytes) -> str | None:
    """Build a signature string like ``def foo(x: int) -> bool`` from the AST."""
    name_node = func_node.child_by_field_name("name")
    params_node = func_node.child_by_field_name("parameters")
    if name_node is None or params_node is None:
        return None

    is_async = _is_async_function(func_node)
    prefix = "async def" if is_async else "def"
    name = _node_text(name_node, source)
    params = _node_text(params_node, source)

    # Return type annotation: look for a 'type' node after '->' in children.
    return_type = ""
    for child in func_node.children:
        if child.type == "type":
            return_type = f" -> {_node_text(child, source)}"
            break

    return f"{prefix} {name}{params}{return_type}"


def _extract_python_docstring(func_or_class_node, source: bytes) -> str | None:
    """Extract the first line of the docstring from a function or class body.

    In Python's AST, a docstring is the first ``expression_statement``
    child of the ``block`` that contains a ``string`` node.
    """
    body = func_or_class_node.child_by_field_name("body")
    if body is None:
        return None
    for child in body.children:
        if child.type == "expression_statement":
            for sub in child.children:
                if sub.type == "string":
                    raw = _node_text(sub, source)
                    # Strip quotes (""", ''', ", ')
                    for q in ('"""', "'''", '"', "'"):
                        if raw.startswith(q) and raw.endswith(q):
                            raw = raw[len(q) : -len(q)]
                            break
                    first_line = raw.strip().split("\n")[0].strip()
                    if not first_line:
                        return None
                    return first_line[:_DOCSTRING_CAP]
            break  # only check the first statement
    return None


def _extract_decorators(node, source: bytes) -> list[str]:
    """Extract decorator strings from a ``decorated_definition`` parent.

    If the function/class is wrapped in a ``decorated_definition``, its
    ``decorator`` children appear as siblings before the actual definition.
    """
    parent = node.parent
    if parent is None or parent.type != "decorated_definition":
        return []
    decorators = []
    for child in parent.children:
        if child.type == "decorator":
            text = _node_text(child, source).strip()
            decorators.append(text)
    return decorators


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
        parent_class = _find_enclosing_class_name(
            func_node, source, "class_definition"
        )
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
                signature=_extract_python_signature(func_node, source),
                docstring=_extract_python_docstring(func_node, source),
                decorators=_extract_decorators(func_node, source),
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
                docstring=_extract_python_docstring(class_node, source),
                decorators=_extract_decorators(class_node, source),
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


def _extract_ts_js(source: bytes, tree, language: str) -> FileStructure:
    """Shared extractor for TypeScript and JavaScript.

    Handles function_declaration, class_declaration, method_definition,
    arrow functions bound via ``const foo = () => ...``, and imports.
    TS additionally captures interface_declaration; JS queries don't, so that
    capture bucket is simply empty for JS files.
    """
    query = _load_query(language)
    if query is None:
        return FileStructure()

    structure = FileStructure()
    captures = QueryCursor(query).captures(tree.root_node)

    # Top-level function declarations
    for func_node in _nodes_from_captures(captures, "function.body"):
        name_node = func_node.child_by_field_name("name")
        if name_node is None:
            continue
        is_async = _is_async_function(func_node)
        kind = "async_function" if is_async else "function"
        structure.functions.append(
            Symbol(
                name=_node_text(name_node, source),
                kind=kind,
                start_line=func_node.start_point[0] + 1,
                end_line=func_node.end_point[0] + 1,
            )
        )

    # Class declarations
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

    # Interface declarations (TS only; JS query doesn't capture these)
    for iface_node in _nodes_from_captures(captures, "interface.body"):
        name_node = iface_node.child_by_field_name("name")
        if name_node is None:
            continue
        structure.classes.append(
            Symbol(
                name=_node_text(name_node, source),
                kind="interface",
                start_line=iface_node.start_point[0] + 1,
                end_line=iface_node.end_point[0] + 1,
            )
        )

    # Method definitions inside classes
    for method_node in _nodes_from_captures(captures, "method.body"):
        name_node = method_node.child_by_field_name("name")
        if name_node is None:
            continue
        parent = _find_enclosing_class_name(
            method_node, source, "class_declaration"
        )
        is_async = _is_async_function(method_node)
        kind = "async_method" if is_async else "method"
        structure.functions.append(
            Symbol(
                name=_node_text(name_node, source),
                kind=kind,
                start_line=method_node.start_point[0] + 1,
                end_line=method_node.end_point[0] + 1,
                parent_class=parent,
            )
        )

    # Arrow functions bound to a const/let/var
    for decl_node in _nodes_from_captures(captures, "arrow.decl"):
        name_node = decl_node.child_by_field_name("name")
        value_node = decl_node.child_by_field_name("value")
        if name_node is None or value_node is None:
            continue
        is_async = _is_async_function(value_node)
        kind = "async_function" if is_async else "function"
        structure.functions.append(
            Symbol(
                name=_node_text(name_node, source),
                kind=kind,
                start_line=decl_node.start_point[0] + 1,
                end_line=decl_node.end_point[0] + 1,
            )
        )

    # Imports
    for imp_node in _nodes_from_captures(captures, "import"):
        structure.imports.append(
            ImportStmt(
                raw=_node_text(imp_node, source),
                start_line=imp_node.start_point[0] + 1,
            )
        )

    return structure


def _extract_typescript(source: bytes, tree) -> FileStructure:
    return _extract_ts_js(source, tree, "typescript")


def _extract_javascript(source: bytes, tree) -> FileStructure:
    return _extract_ts_js(source, tree, "javascript")


_EXTRACTORS = {
    "python": _extract_python,
    "typescript": _extract_typescript,
    "javascript": _extract_javascript,
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
