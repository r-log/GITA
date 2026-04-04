"""
Deterministic code parsers — extract structure from source files without LLM.

Python: uses ast module for precise extraction.
JavaScript/TypeScript: regex-based extraction.
Config (JSON/YAML/TOML): stdlib parsing.
Generic: line count and TODO scanning only.
"""

import ast
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import structlog

log = structlog.get_logger()

# Language detection by extension
EXTENSION_MAP = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".json": "json",
    ".yml": "yaml", ".yaml": "yaml",
    ".toml": "toml",
    ".md": "markdown", ".rst": "markdown",
    ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "css", ".sass": "css",
    ".sql": "sql",
    ".graphql": "graphql", ".gql": "graphql",
    ".sh": "shell", ".bash": "shell",
    ".dockerfile": "docker",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "c_sharp",
    ".kt": "kotlin", ".kts": "kotlin",
    ".vue": "vue",
    ".svelte": "svelte",
}

# Files with no extension that have known types
FILENAME_MAP = {
    "dockerfile": "docker",
    "makefile": "shell",
    "procfile": "config",
    "gemfile": "ruby",
    ".env.example": "config",
    ".gitignore": "config",
}


@dataclass
class FileIndex:
    """Standardized output from all parsers."""
    file_path: str
    language: str
    size_bytes: int
    line_count: int
    structure: dict = field(default_factory=dict)
    content_hash: str = ""


def detect_language(file_path: str) -> str:
    """Detect language from file extension or name."""
    lower = file_path.lower()
    filename = lower.split("/")[-1]

    if filename in FILENAME_MAP:
        return FILENAME_MAP[filename]

    ext = Path(lower).suffix
    return EXTENSION_MAP.get(ext, "other")


def compute_hash(content: str) -> str:
    """SHA256 hash of file content."""
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


def _extract_todos(content: str) -> list[dict]:
    """Extract TODO/FIXME comments from any file.

    Only matches markers that appear inside real comments (lines starting with
    comment characters), not inside strings or code. Requires the marker to be
    followed by : or whitespace to avoid matching variable names like 'all_todos'.
    """
    import re
    # Pattern: marker followed by colon, whitespace, or end-of-line
    marker_re = re.compile(r'\b(TODO|FIXME|HACK|XXX)\b[\s:(-]')

    todos = []
    for i, line in enumerate(content.split("\n"), 1):
        stripped = line.lstrip()

        # Only consider lines that are clearly comments (start with comment char)
        is_comment = (
            stripped.startswith("#") or
            stripped.startswith("//") or
            stripped.startswith("/*") or
            stripped.startswith("*") or
            stripped.startswith("<!--")
        )
        if not is_comment:
            continue

        match = marker_re.search(stripped)
        if match:
            text = stripped[match.start():].strip()
            todos.append({"line": i, "text": text[:200]})
    return todos


# ── Python Parser ──────────────────────────────────────────────────

def parse_python(content: str, file_path: str) -> FileIndex:
    """Parse Python file using ast module."""
    lines = content.split("\n")
    result = FileIndex(
        file_path=file_path,
        language="python",
        size_bytes=len(content.encode("utf-8")),
        line_count=len(lines),
        content_hash=compute_hash(content),
    )

    try:
        tree = ast.parse(content)
    except SyntaxError:
        log.debug("ast_parse_failed", file=file_path)
        result.structure = {
            "parse_error": True,
            "todos": _extract_todos(content),
        }
        return result

    imports = []
    classes = []
    functions = []
    routes = []
    constants = []

    for node in ast.walk(tree):
        # Imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imports.append(module)

        # Top-level classes
        elif isinstance(node, ast.ClassDef):
            methods = []
            bases = [_get_name(b) for b in node.bases]
            decorators = [_get_decorator_name(d) for d in node.decorator_list]
            fields = []

            for item in node.body:
                if isinstance(item, ast.FunctionDef) or isinstance(item, ast.AsyncFunctionDef):
                    args = [a.arg for a in item.args.args if a.arg != "self"]
                    method_decorators = [_get_decorator_name(d) for d in item.decorator_list]
                    methods.append({
                        "name": item.name,
                        "args": args,
                        "decorators": method_decorators,
                        "line": item.lineno,
                    })
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    fields.append(item.target.id)
                elif isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name):
                            fields.append(target.id)

            classes.append({
                "name": node.name,
                "bases": bases,
                "decorators": decorators,
                "methods": [m["name"] for m in methods],
                "method_details": methods,
                "fields": fields[:20],
                "line": node.lineno,
            })

        # Top-level functions
        elif isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
            # Only top-level (not methods inside classes)
            if not _is_inside_class(tree, node):
                args = [a.arg for a in node.args.args if a.arg != "self"]
                decorators = [_get_decorator_name(d) for d in node.decorator_list]
                functions.append({
                    "name": node.name,
                    "args": args,
                    "decorators": decorators,
                    "is_async": isinstance(node, ast.AsyncFunctionDef),
                    "line": node.lineno,
                })

                # Detect route decorators
                for dec in decorators:
                    if any(kw in dec for kw in (".route", ".get", ".post", ".put", ".delete", ".patch")):
                        route_path = _extract_route_path(node)
                        method = _guess_http_method(dec)
                        # For .route() decorators, check methods= kwarg
                        if method == "ANY":
                            method = _extract_flask_methods(node) or "ANY"
                        routes.append({
                            "method": method,
                            "path": route_path or dec,
                            "handler": node.name,
                            "line": node.lineno,
                        })

        # Top-level constants
        elif isinstance(node, ast.Assign) and _is_top_level(tree, node):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    value = _get_constant_value(node.value)
                    if value is not None:
                        constants.append({"name": target.id, "value": str(value)[:100]})

    result.structure = {
        "imports": sorted(set(imports)),
        "classes": classes,
        "functions": functions,
        "routes": routes,
        "constants": constants[:20],
        "todos": _extract_todos(content),
    }
    return result


def _get_name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    elif isinstance(node, ast.Attribute):
        return f"{_get_name(node.value)}.{node.attr}"
    return str(node)


def _get_decorator_name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    elif isinstance(node, ast.Attribute):
        return f"{_get_name(node.value)}.{node.attr}"
    elif isinstance(node, ast.Call):
        return _get_decorator_name(node.func)
    return ""


def _is_inside_class(tree, target_node) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in ast.walk(node):
                if child is target_node and child is not node:
                    return True
    return False


def _is_top_level(tree, node) -> bool:
    return node in ast.iter_child_nodes(tree)


def _extract_route_path(node) -> str | None:
    for dec in node.decorator_list:
        if isinstance(dec, ast.Call) and dec.args:
            arg = dec.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return arg.value
    return None


def _guess_http_method(decorator: str) -> str:
    for method in ("get", "post", "put", "delete", "patch"):
        if f".{method}" in decorator.lower():
            return method.upper()
    return "ANY"


def _extract_flask_methods(node) -> str | None:
    """Extract HTTP method from Flask @bp.route('/path', methods=['POST']) kwarg."""
    for dec in node.decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        for kw in dec.keywords:
            if kw.arg == "methods" and isinstance(kw.value, ast.List):
                methods = []
                for elt in kw.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        methods.append(elt.value.upper())
                if methods:
                    return ", ".join(methods)
    return None


def _get_constant_value(node):
    if isinstance(node, ast.Constant):
        return node.value
    return None


# ── JavaScript/TypeScript Parser ───────────────────────────────────

# Patterns for JS/TS extraction
JS_IMPORT = re.compile(r"""(?:import\s+(?:{[^}]+}|[\w*]+(?:\s+as\s+\w+)?)\s+from\s+['"]([^'"]+)['"]|require\s*\(\s*['"]([^'"]+)['"]\s*\))""")
JS_EXPORT = re.compile(r"""export\s+(?:default\s+)?(?:function|class|const|let|var|async\s+function)\s+(\w+)""")
JS_FUNCTION = re.compile(r"""(?:(?:export\s+)?(?:async\s+)?function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>)""")
JS_CLASS = re.compile(r"""(?:export\s+)?class\s+(\w+)(?:\s+extends\s+(\w+))?""")
JS_ROUTE = re.compile(r"""(?:app|router|server)\.(get|post|put|delete|patch|use)\s*\(\s*['"]([^'"]+)['"]""", re.IGNORECASE)
JS_FETCH = re.compile(r"""fetch\s*\(\s*[`'"](\/[^'"`]+)[`'"]""")
JS_COMPONENT = re.compile(r"""(?:function|const)\s+([A-Z]\w+)\s*(?:=\s*(?:React\.)?(?:memo|forwardRef)\s*\()?\s*\(""")


def parse_javascript(content: str, file_path: str, override_language: str | None = None) -> FileIndex:
    """Parse JavaScript/TypeScript file using regex."""
    lines = content.split("\n")
    if override_language:
        language = override_language
    elif file_path.endswith((".ts", ".tsx")):
        language = "typescript"
    else:
        language = "javascript"

    imports = []
    for m in JS_IMPORT.finditer(content):
        imports.append(m.group(1) or m.group(2))

    exports = [m.group(1) for m in JS_EXPORT.finditer(content)]

    functions = []
    for m in JS_FUNCTION.finditer(content):
        name = m.group(1) or m.group(2)
        if name:
            functions.append({"name": name, "line": content[:m.start()].count("\n") + 1})

    classes = []
    for m in JS_CLASS.finditer(content):
        classes.append({
            "name": m.group(1),
            "extends": m.group(2),
            "line": content[:m.start()].count("\n") + 1,
        })

    routes = []
    for m in JS_ROUTE.finditer(content):
        routes.append({"method": m.group(1).upper(), "path": m.group(2)})

    components = [m.group(1) for m in JS_COMPONENT.finditer(content)]

    api_calls = [m.group(1) for m in JS_FETCH.finditer(content)]

    return FileIndex(
        file_path=file_path,
        language=language,
        size_bytes=len(content.encode("utf-8")),
        line_count=len(lines),
        content_hash=compute_hash(content),
        structure={
            "imports": sorted(set(imports)),
            "exports": exports,
            "functions": functions,
            "classes": classes,
            "routes": routes,
            "components": components,
            "api_calls": api_calls,
            "todos": _extract_todos(content),
        },
    )


# ── Config Parser ─────────────────────────────────────────────────

def parse_config(content: str, file_path: str) -> FileIndex:
    """Parse JSON/YAML/TOML config files."""
    lines = content.split("\n")
    language = detect_language(file_path)
    structure = {}

    try:
        if language == "json":
            data = json.loads(content)
            structure = _summarize_config(data, file_path)
        elif language == "yaml":
            import yaml
            data = yaml.safe_load(content)
            if isinstance(data, dict):
                structure = _summarize_config(data, file_path)
        elif language == "toml":
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib  # Python 3.10 fallback
            data = tomllib.loads(content)
            structure = _summarize_config(data, file_path)
    except Exception:
        structure = {"parse_error": True}

    structure["todos"] = _extract_todos(content)

    return FileIndex(
        file_path=file_path,
        language=language,
        size_bytes=len(content.encode("utf-8")),
        line_count=len(lines),
        content_hash=compute_hash(content),
        structure=structure,
    )


def _summarize_config(data: dict, file_path: str) -> dict:
    """Extract key information from parsed config."""
    filename = file_path.lower().split("/")[-1]
    result = {"top_level_keys": list(data.keys())[:30]}

    # Special handling for package.json
    if filename == "package.json":
        result["name"] = data.get("name", "")
        result["scripts"] = list(data.get("scripts", {}).keys())
        result["dependencies"] = list(data.get("dependencies", {}).keys())
        result["dev_dependencies"] = list(data.get("devDependencies", {}).keys())

    # Special handling for pyproject.toml
    elif filename == "pyproject.toml":
        project = data.get("project", {})
        result["name"] = project.get("name", "")
        result["dependencies"] = project.get("dependencies", [])
        tool = data.get("tool", {})
        if "poetry" in tool:
            deps = tool["poetry"].get("dependencies", {})
            result["dependencies"] = list(deps.keys()) if isinstance(deps, dict) else deps

    # Special handling for docker-compose
    elif "docker-compose" in filename:
        result["services"] = list(data.get("services", {}).keys())

    return result


# ── Generic Parser ─────────────────────────────────────────────────

def parse_generic(content: str, file_path: str) -> FileIndex:
    """Fallback parser — just line count, size, and TODOs."""
    lines = content.split("\n")
    return FileIndex(
        file_path=file_path,
        language=detect_language(file_path),
        size_bytes=len(content.encode("utf-8")),
        line_count=len(lines),
        content_hash=compute_hash(content),
        structure={"todos": _extract_todos(content)},
    )


# ── Main Parser Dispatcher ────────────────────────────────────────

# Languages that tree-sitter handles (when available)
TREE_SITTER_LANGUAGES = {"go", "java", "rust", "c_sharp", "ruby", "php"}


def parse_file(content: str, file_path: str) -> FileIndex:
    """Parse a file using the appropriate parser based on language."""
    language = detect_language(file_path)

    try:
        if language == "python":
            return parse_python(content, file_path)
        elif language in ("javascript", "typescript"):
            return parse_javascript(content, file_path)
        elif language in ("json", "yaml", "toml"):
            return parse_config(content, file_path)
        elif language == "vue":
            return parse_javascript(content, file_path, override_language="vue")
        elif language in TREE_SITTER_LANGUAGES:
            return _parse_with_tree_sitter(content, file_path, language)
        else:
            return parse_generic(content, file_path)
    except Exception as e:
        log.warning("parser_error", file=file_path, error=str(e))
        return parse_generic(content, file_path)


def _parse_with_tree_sitter(content: str, file_path: str, language: str) -> FileIndex:
    """Parse using tree-sitter, fall back to generic if not available."""
    from src.indexer.tree_sitter_parser import is_available, parse_tree_sitter

    lines = content.split("\n")
    structure = {}

    if is_available():
        structure = parse_tree_sitter(content, file_path, language)

    # Always add TODOs (tree-sitter doesn't extract these)
    structure["todos"] = _extract_todos(content)

    return FileIndex(
        file_path=file_path,
        language=language,
        size_bytes=len(content.encode("utf-8")),
        line_count=len(lines),
        content_hash=compute_hash(content),
        structure=structure,
    )
