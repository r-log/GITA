"""File parsing — produces a FileStructure from source code.

Each supported language has an extractor that walks a Tree-sitter parse tree
and emits functions, classes, and imports with accurate 1-indexed start/end
line numbers. The public entry point is ``parse_file``.
"""

from __future__ import annotations

import logging
import re
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
    # "function" | "method" | "async_function" | "async_method" | "class" |
    # "interface" | "struct" | "union" | "enum" | "record"
    kind: str
    start_line: int  # 1-indexed
    end_line: int  # 1-indexed, inclusive
    parent_class: str | None = None
    signature: str | None = None  # e.g. "def foo(x: int, y: str) -> bool"
    docstring: str | None = None  # first line, capped at _DOCSTRING_CAP chars
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

# Grammars that share another language's query file (TSX is TypeScript plus
# JSX: same node types for everything the query captures).
_QUERY_FILE: dict[str, str] = {"tsx": "typescript"}


def _load_query(language: str) -> Query | None:
    if language in _QUERY_CACHE:
        return _QUERY_CACHE[language]
    lang = get_language(language)
    if lang is None:
        return None
    query_path = QUERIES_DIR / f"{_QUERY_FILE.get(language, language)}.scm"
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


def _find_enclosing_class_name(node, source: bytes, class_node_type: str) -> str | None:
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
    return any(child.type == "async" for child in node.children)


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
        parent_class = _find_enclosing_class_name(func_node, source, "class_definition")
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


def _extract_ts_js_signature(
    node, source: bytes, *, name: str | None = None, is_async: bool = False
) -> str | None:
    """Build a signature from a TS/JS function/method node.

    Looks for ``formal_parameters`` and an optional trailing ``type_annotation``
    (return type, TS only).
    """
    params_node = node.child_by_field_name("parameters")
    if params_node is None:
        return None
    if name is None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = _node_text(name_node, source)

    params = _node_text(params_node, source)
    prefix = "async function" if is_async else "function"

    # Return type: look for type_annotation child after formal_parameters.
    # Tree-sitter node wrappers may differ in identity, so compare by type
    # and position rather than using ``is``.
    return_type = ""
    past_params = False
    for child in node.children:
        if child.type == "formal_parameters":
            past_params = True
            continue
        if past_params and child.type == "type_annotation":
            return_type = _node_text(child, source)  # includes ": Type"
            break

    return f"{prefix} {name}{params}{return_type}"


def _extract_jsdoc(node, source: bytes) -> str | None:
    """Extract the first line of a JSDoc comment preceding a node.

    Looks for a ``comment`` sibling immediately before the node whose text
    starts with ``/**``. If the node is wrapped in ``export_statement``,
    the comment may be a sibling of the export wrapper instead.

    Returns the first meaningful line (stripped of ``*`` prefixes and
    whitespace), capped at ``_DOCSTRING_CAP`` chars.
    """
    # Try direct prev sibling first, then parent's prev sibling
    # (handles `export function ...` where comment precedes the export).
    candidates = [node]
    if node.parent and node.parent.type in ("export_statement", "export_default_declaration"):
        candidates.append(node.parent)

    for candidate in candidates:
        prev = candidate.prev_named_sibling
        if prev is None or prev.type != "comment":
            continue
        text = _node_text(prev, source)
        if not text.startswith("/**"):
            continue
        # Strip /** ... */ and * line prefixes
        text = text[3:]
        if text.endswith("*/"):
            text = text[:-2]
        for line in text.split("\n"):
            stripped = line.strip().lstrip("*").strip()
            if stripped and not stripped.startswith("@"):
                return stripped[:_DOCSTRING_CAP]
    return None


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
                signature=_extract_ts_js_signature(func_node, source, is_async=is_async),
                docstring=_extract_jsdoc(func_node, source),
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
                docstring=_extract_jsdoc(class_node, source),
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
                docstring=_extract_jsdoc(iface_node, source),
            )
        )

    # Method definitions inside classes
    for method_node in _nodes_from_captures(captures, "method.body"):
        name_node = method_node.child_by_field_name("name")
        if name_node is None:
            continue
        parent = _find_enclosing_class_name(method_node, source, "class_declaration")
        is_async = _is_async_function(method_node)
        kind = "async_method" if is_async else "method"
        name = _node_text(name_node, source)
        structure.functions.append(
            Symbol(
                name=name,
                kind=kind,
                start_line=method_node.start_point[0] + 1,
                end_line=method_node.end_point[0] + 1,
                parent_class=parent,
                signature=_extract_ts_js_signature(
                    method_node, source, name=name, is_async=is_async
                ),
                docstring=_extract_jsdoc(method_node, source),
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
        name = _node_text(name_node, source)
        # Arrow function signature: parameters are on the value (arrow_function) node
        sig = _extract_ts_js_signature(value_node, source, name=name, is_async=is_async)
        # JSDoc for arrow: comment sibling is before the lexical_declaration parent
        parent_decl = decl_node.parent  # lexical_declaration (const/let/var)
        docstring = _extract_jsdoc(parent_decl, source) if parent_decl else None
        structure.functions.append(
            Symbol(
                name=name,
                kind=kind,
                start_line=decl_node.start_point[0] + 1,
                end_line=decl_node.end_point[0] + 1,
                signature=sig,
                docstring=docstring,
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


# ---------------------------------------------------------------------------
# C and C++
# ---------------------------------------------------------------------------
_C_TYPE_KINDS = {
    "class_specifier": "class",
    "struct_specifier": "struct",
    "union_specifier": "union",
    "enum_specifier": "enum",
}
# Declarators that wrap the function declarator: `T* f()`, `T& C::f()`, `(f)()`.
_C_DECLARATOR_WRAPPERS = frozenset(
    {"pointer_declarator", "reference_declarator", "parenthesized_declarator"}
)
# Wrappers whose comment belongs to the definition they wrap.
_C_DEFINITION_WRAPPERS = frozenset(
    {"template_declaration", "field_declaration", "declaration", "type_definition"}
)
_SIGNATURE_CAP = 300


def _collapse(text: str) -> str:
    return " ".join(text.split())[:_SIGNATURE_CAP]


def _c_function_declarator(func_node):
    node = func_node.child_by_field_name("declarator")
    while node is not None and node.type in _C_DECLARATOR_WRAPPERS:
        inner = node.child_by_field_name("declarator")
        if inner is None:  # parenthesized_declarator has no field name
            inner = next(iter(node.named_children), None)
        node = inner
    return node if node is not None and node.type == "function_declarator" else None


def _c_name_and_scope(declarator, source: bytes) -> tuple[str, str | None]:
    """``Game::Player::Tick`` -> ("Tick", "Player"); ``~Player`` -> ("~Player", None)."""
    target = declarator.child_by_field_name("declarator")
    if target is None:
        return _node_text(declarator, source), None
    scope: str | None = None
    while target.type == "qualified_identifier":
        scope_node = target.child_by_field_name("scope")
        if scope_node is not None:
            scope = _node_text(scope_node, source).split("<", 1)[0]
        name_node = target.child_by_field_name("name")
        if name_node is None:
            break
        target = name_node
    return _node_text(target, source), scope


def _c_enclosing_type(node, source: bytes) -> str | None:
    current = node.parent
    while current is not None:
        if current.type in _C_TYPE_KINDS:
            name = current.child_by_field_name("name")
            return _node_text(name, source).split("<", 1)[0] if name is not None else None
        current = current.parent
    return None


def _definition_anchor(node, wrappers: frozenset[str]):
    """The outermost wrapper starting on the same line (``template <...>`` may
    start a line earlier) — that's where a doc comment sits."""
    anchor = node
    while anchor.parent is not None and anchor.parent.type in wrappers:
        parent = anchor.parent
        if parent.type != "template_declaration" and parent.start_point[0] != anchor.start_point[0]:
            break
        anchor = parent
    return anchor


_DOC_DECORATION = re.compile(r"[-=*/#~_+\s]*")
_DOC_PREFIX = re.compile(r"^(?:/\*\*|/\*!|/\*|///|//!|//)\s?")
_XML_TAG = re.compile(r"</?[A-Za-z][^>]*>")


def _comment_block(anchor, source: bytes) -> str | None:
    """Text of the comments directly above ``anchor`` (no blank line between)."""
    parts: list[str] = []
    expect_end = anchor.start_point[0] - 1
    prev = anchor.prev_sibling
    while prev is not None and prev.type == "comment" and prev.end_point[0] == expect_end:
        before = prev.prev_sibling
        if before is not None and before.end_point[0] == prev.start_point[0]:
            break  # a trailing comment on the previous statement
        parts.insert(0, _node_text(prev, source))
        expect_end = prev.start_point[0] - 1
        prev = before
    return "\n".join(parts) if parts else None


def _first_doc_line(text: str | None) -> str | None:
    """First meaningful line of a // , /// , /* */ or /** */ comment block."""
    if not text:
        return None
    for raw in text.splitlines():
        line = _DOC_PREFIX.sub("", raw.strip())
        line = line.removesuffix("*/").strip().lstrip("*").strip()
        line = _XML_TAG.sub("", line).strip()
        for tag in ("@brief", "\\brief"):
            if line.startswith(tag):
                line = line[len(tag) :].strip()
        if not line or _DOC_DECORATION.fullmatch(line) or line[0] in "@\\":
            continue
        return line[:_DOCSTRING_CAP]
    return None


def _c_signature(func_node, source: bytes, start_node) -> str | None:
    """Everything before the body, minus a constructor's initializer list."""
    end = func_node.child_by_field_name("body")
    for child in func_node.children:
        if child.type == "field_initializer_list":
            end = child
            break
    if end is None:
        return None
    return _collapse(source[start_node.start_byte : end.start_byte].decode("utf-8", "replace"))


def _extract_c_family(source: bytes, tree, language: str) -> FileStructure:
    """C and C++: functions, methods (inline or ``Class::method``), types, includes."""
    query = _load_query(language)
    if query is None:
        return FileStructure()
    structure = FileStructure()
    captures = QueryCursor(query).captures(tree.root_node)

    for func_node in _nodes_from_captures(captures, "function.body"):
        declarator = _c_function_declarator(func_node)
        if declarator is None:
            continue  # a macro the grammar couldn't see through
        name, scope = _c_name_and_scope(declarator, source)
        parent_class = scope or _c_enclosing_type(func_node, source)
        anchor = _definition_anchor(func_node, frozenset({"template_declaration"}))
        structure.functions.append(
            Symbol(
                name=name,
                kind="method" if parent_class else "function",
                start_line=anchor.start_point[0] + 1,
                end_line=func_node.end_point[0] + 1,
                parent_class=parent_class,
                signature=_c_signature(func_node, source, anchor),
                docstring=_first_doc_line(_comment_block(anchor, source)),
            )
        )

    for type_node in _nodes_from_captures(captures, "class.body"):
        name_node = type_node.child_by_field_name("name")
        if name_node is None:
            continue  # anonymous struct/union/enum
        anchor = _definition_anchor(type_node, _C_DEFINITION_WRAPPERS)
        structure.classes.append(
            Symbol(
                name=_node_text(name_node, source).split("<", 1)[0],
                kind=_C_TYPE_KINDS[type_node.type],
                start_line=anchor.start_point[0] + 1,
                end_line=type_node.end_point[0] + 1,
                parent_class=_c_enclosing_type(type_node, source),
                docstring=_first_doc_line(_comment_block(anchor, source)),
            )
        )

    for imp_node in _nodes_from_captures(captures, "import"):
        structure.imports.append(
            ImportStmt(
                raw=_node_text(imp_node, source).strip(), start_line=imp_node.start_point[0] + 1
            )
        )
    _sort_by_line(structure)
    return structure


def _sort_by_line(structure: FileStructure) -> None:
    structure.functions.sort(key=lambda sym: (sym.start_line, sym.name))
    structure.classes.sort(key=lambda sym: (sym.start_line, sym.name))
    structure.imports.sort(key=lambda imp: imp.start_line)


def _extract_c(source: bytes, tree) -> FileStructure:
    return _extract_c_family(source, tree, "c")


def _extract_cpp(source: bytes, tree) -> FileStructure:
    return _extract_c_family(source, tree, "cpp")


# ---------------------------------------------------------------------------
# C#
# ---------------------------------------------------------------------------
_CS_TYPE_KINDS = {
    "class_declaration": "class",
    "struct_declaration": "struct",
    "interface_declaration": "interface",
    "record_declaration": "record",
    "enum_declaration": "enum",
}


def _cs_enclosing_type(node, source: bytes) -> str | None:
    current = node.parent
    while current is not None:
        if current.type in _CS_TYPE_KINDS:
            name = current.child_by_field_name("name")
            return _node_text(name, source) if name is not None else None
        current = current.parent
    return None


def _cs_signature(node, source: bytes) -> str | None:
    """Modifiers, return type, name and parameters; attributes and body left out."""
    start = next((c for c in node.children if c.type != "attribute_list"), None)
    end = node.child_by_field_name("body")
    if end is None:
        end = next((c for c in node.children if c.type in ("arrow_expression_clause", ";")), None)
    if start is None:
        return None
    stop = end.start_byte if end is not None else node.end_byte
    return _collapse(source[start.start_byte : stop].decode("utf-8", "replace"))


def _extract_csharp(source: bytes, tree) -> FileStructure:
    query = _load_query("csharp")
    if query is None:
        return FileStructure()
    structure = FileStructure()
    captures = QueryCursor(query).captures(tree.root_node)

    for node in _nodes_from_captures(captures, "function.body"):
        name_node = node.child_by_field_name("name")
        if name_node is None:
            continue
        parent_class = _cs_enclosing_type(node, source)
        is_async = any(
            c.type == "modifier" and _node_text(c, source) == "async" for c in node.children
        )
        is_method = parent_class is not None and node.type != "local_function_statement"
        kind = "method" if is_method else "function"
        structure.functions.append(
            Symbol(
                name=_node_text(name_node, source),
                kind=f"async_{kind}" if is_async else kind,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                parent_class=parent_class,
                signature=_cs_signature(node, source),
                docstring=_first_doc_line(_comment_block(node, source)),
                decorators=[
                    _collapse(_node_text(c, source))
                    for c in node.children
                    if c.type == "attribute_list"
                ],
            )
        )

    for node in _nodes_from_captures(captures, "class.body"):
        name_node = node.child_by_field_name("name")
        if name_node is None:
            continue
        structure.classes.append(
            Symbol(
                name=_node_text(name_node, source),
                kind=_CS_TYPE_KINDS[node.type],
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                parent_class=_cs_enclosing_type(node, source),
                docstring=_first_doc_line(_comment_block(node, source)),
                decorators=[
                    _collapse(_node_text(c, source))
                    for c in node.children
                    if c.type == "attribute_list"
                ],
            )
        )

    for imp_node in _nodes_from_captures(captures, "import"):
        structure.imports.append(
            ImportStmt(
                raw=_node_text(imp_node, source).strip(), start_line=imp_node.start_point[0] + 1
            )
        )
    _sort_by_line(structure)
    return structure


_EXTRACTORS = {
    "python": _extract_python,
    "typescript": _extract_typescript,
    "tsx": lambda source, tree: _extract_ts_js(source, tree, "tsx"),
    "javascript": _extract_javascript,
    "c": _extract_c,
    "cpp": _extract_cpp,
    "csharp": _extract_csharp,
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
