"""
Tree-sitter based parser for languages beyond Python/JS.

Provides grammar-based parsing for Go, Java, Rust, C#, Ruby, PHP.
Python uses stdlib `ast` (100% accurate), JS uses regex (proven on real repos).
This module handles everything else with real grammars.

Requires: pip install tree-sitter-languages
Falls back to generic parser if not installed.
"""

from __future__ import annotations

import structlog

log = structlog.get_logger()

try:
    from tree_sitter_languages import get_parser as _get_parser
    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False
    log.info("tree_sitter_not_available", msg="Install tree-sitter-languages for Go/Java/Rust/C#/Ruby/PHP parsing")


# Map our language names to tree-sitter grammar names
_GRAMMAR_MAP = {
    "go": "go",
    "java": "java",
    "rust": "rust",
    "c_sharp": "c_sharp",
    "ruby": "ruby",
    "php": "php",
}


def is_available() -> bool:
    return TREE_SITTER_AVAILABLE


def parse_tree_sitter(content: str, file_path: str, language: str) -> dict:
    """
    Parse source code using tree-sitter and extract structure.

    Returns a dict with: imports, classes, functions, routes, todos, etc.
    """
    grammar = _GRAMMAR_MAP.get(language)
    if not grammar or not TREE_SITTER_AVAILABLE:
        return {}

    try:
        parser = _get_parser(grammar)
        tree = parser.parse(content.encode("utf-8"))
    except Exception as e:
        log.warning("tree_sitter_parse_error", file=file_path, language=language, error=str(e))
        return {}

    root = tree.root_node

    if language == "go":
        return _extract_go(root)
    elif language == "java":
        return _extract_java(root)
    elif language == "rust":
        return _extract_rust(root)
    elif language == "c_sharp":
        return _extract_csharp(root)
    elif language == "ruby":
        return _extract_ruby(root)
    elif language == "php":
        return _extract_php(root)
    return {}


# ── Helpers ───────────────────────────────────────────────────────


def _text(node) -> str:
    """Get node text as string."""
    return node.text.decode("utf-8", errors="replace")


def _find(node, *types) -> str | None:
    """Find first child matching any of the given types, return its text."""
    for child in node.children:
        if child.type in types:
            return _text(child)
    return None


def _find_node(node, *types):
    """Find first child node matching any of the given types."""
    for child in node.children:
        if child.type in types:
            return child
    return None


def _walk(node):
    """Recursively yield all descendant nodes."""
    for child in node.children:
        yield child
        yield from _walk(child)


def _children_of_type(node, *types):
    """Yield direct children matching any of the given types."""
    for child in node.children:
        if child.type in types:
            yield child


# ── Go ────────────────────────────────────────────────────────────


def _extract_go(root) -> dict:
    imports = []
    structs = []
    interfaces = []
    functions = []
    methods = []
    routes = []

    for node in root.children:
        if node.type == "import_declaration":
            # Single import or import block
            for spec in _walk(node):
                if spec.type == "import_spec":
                    path = _find(spec, "interpreted_string_literal")
                    if path:
                        imports.append(path.strip('"'))
                elif spec.type == "interpreted_string_literal" and not any(c.type == "import_spec" for c in _walk(node)):
                    imports.append(_text(spec).strip('"'))

        elif node.type == "type_declaration":
            for spec in _children_of_type(node, "type_spec"):
                name = _find(spec, "type_identifier")
                type_node = _find_node(spec, "struct_type", "interface_type")
                if type_node and type_node.type == "struct_type":
                    fields = _extract_go_struct_fields(type_node)
                    structs.append({"name": name, "fields": fields})
                elif type_node and type_node.type == "interface_type":
                    iface_methods = []
                    for ms in _walk(type_node):
                        if ms.type == "method_spec":
                            mname = _find(ms, "field_identifier")
                            if mname:
                                iface_methods.append(mname)
                    interfaces.append({"name": name, "methods": iface_methods})

        elif node.type == "function_declaration":
            name = _find(node, "identifier")
            params = _extract_go_params(node)
            if name:
                functions.append({
                    "name": name,
                    "args": params,
                    "is_async": False,
                    "line": node.start_point[0] + 1,
                })

        elif node.type == "method_declaration":
            name = _find(node, "field_identifier")
            receiver = _find_node(node, "parameter_list")
            receiver_type = ""
            if receiver:
                for t in _walk(receiver):
                    if t.type == "type_identifier":
                        receiver_type = _text(t)
                        break
            params = _extract_go_params(node)
            if name:
                methods.append({
                    "name": name,
                    "receiver": receiver_type,
                    "args": params,
                    "line": node.start_point[0] + 1,
                })

    # Detect routes from function bodies (Gin/Echo/Mux patterns)
    for node in _walk(root):
        if node.type == "call_expression":
            fn = _find_node(node, "selector_expression")
            if fn:
                method_name = _find(fn, "field_identifier")
                if method_name and method_name.upper() in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
                    args = _find_node(node, "argument_list")
                    if args:
                        path = _find(args, "interpreted_string_literal")
                        handler = None
                        # Second arg is typically the handler
                        arg_children = [c for c in args.children if c.type not in ("(", ")", ",")]
                        if len(arg_children) >= 2:
                            handler = _text(arg_children[1])
                        routes.append({
                            "method": method_name.upper(),
                            "path": path.strip('"') if path else "?",
                            "handler": handler or "?",
                            "line": node.start_point[0] + 1,
                        })

    # Build classes from structs + methods
    classes = []
    for s in structs:
        struct_methods = [m["name"] for m in methods if m["receiver"] == s["name"]]
        classes.append({
            "name": s["name"],
            "bases": [],
            "fields": s["fields"],
            "methods": struct_methods,
        })

    for iface in interfaces:
        classes.append({
            "name": iface["name"],
            "bases": ["interface"],
            "fields": [],
            "methods": iface["methods"],
        })

    return {
        "imports": imports,
        "classes": classes,
        "functions": [f for f in functions if f["name"] != "main"] + [f for f in functions if f["name"] == "main"],
        "routes": routes,
    }


def _extract_go_struct_fields(struct_node) -> list[str]:
    fields = []
    for child in _walk(struct_node):
        if child.type == "field_identifier":
            fields.append(_text(child))
    return fields[:20]


def _extract_go_params(node) -> list[str]:
    params = []
    param_list = None
    # Skip receiver, get the second parameter_list (actual params)
    lists = [c for c in node.children if c.type == "parameter_list"]
    if node.type == "method_declaration" and len(lists) >= 2:
        param_list = lists[1]
    elif lists:
        param_list = lists[0]

    if param_list:
        for decl in _children_of_type(param_list, "parameter_declaration"):
            name = _find(decl, "identifier")
            if name:
                params.append(name)
    return params


# ── Java ──────────────────────────────────────────────────────────


def _extract_java(root) -> dict:
    imports = []
    classes = []
    routes = []

    for node in root.children:
        if node.type == "import_declaration":
            # import com.example.Foo;
            scope = _find_node(node, "scoped_identifier")
            if scope:
                imports.append(_text(scope))

        elif node.type == "class_declaration":
            cls = _extract_java_class(node)
            classes.append(cls)
            # Extract routes from method annotations
            for route in cls.get("_routes", []):
                routes.append(route)

    return {
        "imports": imports,
        "classes": [{k: v for k, v in c.items() if not k.startswith("_")} for c in classes],
        "functions": [],
        "routes": routes,
    }


def _extract_java_class(node) -> dict:
    name = _find(node, "identifier")
    superclass = None
    interfaces = []
    fields = []
    methods = []
    routes = []
    base_path = ""

    # Class-level annotations are in class_declaration > modifiers
    modifiers = _find_node(node, "modifiers")
    if modifiers:
        for child in modifiers.children:
            if child.type in ("marker_annotation", "annotation"):
                ann_name = _find(child, "identifier")
                if ann_name in ("RequestMapping", "Path"):
                    args = _find_node(child, "annotation_argument_list")
                    if args:
                        for n in _walk(args):
                            if n.type == "string_literal":
                                base_path = _text(n).strip('"')
                                break

    # Superclass
    sc = _find_node(node, "superclass")
    if sc:
        superclass = _find(sc, "type_identifier")

    # Interfaces
    ifaces = _find_node(node, "super_interfaces")
    if ifaces:
        for t in _children_of_type(ifaces, "type_identifier"):
            interfaces.append(_text(t))

    bases = []
    if superclass:
        bases.append(superclass)
    bases.extend(interfaces)

    # Body
    body = _find_node(node, "class_body")
    if body:
        for child in body.children:
            if child.type == "field_declaration":
                for decl in _walk(child):
                    if decl.type == "variable_declarator":
                        fname = _find(decl, "identifier")
                        if fname:
                            fields.append(fname)

            elif child.type == "method_declaration":
                mname = _find(child, "identifier")
                if mname:
                    methods.append(mname)

                # Annotations are INSIDE method_declaration > modifiers
                method_mods = _find_node(child, "modifiers")
                if method_mods:
                    route_method = None
                    route_path = ""
                    for ann in method_mods.children:
                        if ann.type not in ("marker_annotation", "annotation"):
                            continue
                        ann_name = _find(ann, "identifier")
                        if not ann_name:
                            continue

                        if ann_name in ("GetMapping", "PostMapping", "PutMapping",
                                        "DeleteMapping", "PatchMapping"):
                            route_method = ann_name.replace("Mapping", "").upper()
                            args = _find_node(ann, "annotation_argument_list")
                            if args:
                                for n in _walk(args):
                                    if n.type == "string_literal":
                                        route_path = _text(n).strip('"')
                                        break
                        elif ann_name == "RequestMapping":
                            route_method = "ANY"
                            args = _find_node(ann, "annotation_argument_list")
                            if args:
                                for n in _walk(args):
                                    if n.type == "string_literal":
                                        route_path = _text(n).strip('"')
                                        break
                        elif ann_name in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                            route_method = ann_name

                    if route_method and mname:
                        full_path = base_path.rstrip("/") + "/" + route_path.lstrip("/") if route_path else base_path or "/"
                        routes.append({
                            "method": route_method,
                            "path": full_path,
                            "handler": mname,
                            "line": child.start_point[0] + 1,
                        })

    return {
        "name": name or "?",
        "bases": bases,
        "fields": fields[:20],
        "methods": methods,
        "_routes": routes,
    }


# ── Rust ──────────────────────────────────────────────────────────


def _extract_rust(root) -> dict:
    imports = []
    structs = []
    enums = []
    functions = []
    impl_methods: dict[str, list[str]] = {}
    routes = []

    for node in root.children:
        if node.type == "use_declaration":
            imports.append(_text(node).removeprefix("use ").removesuffix(";").strip())

        elif node.type == "struct_item":
            name = _find(node, "type_identifier")
            fields = []
            body = _find_node(node, "field_declaration_list")
            if body:
                for fd in _children_of_type(body, "field_declaration"):
                    fname = _find(fd, "field_identifier")
                    if fname:
                        fields.append(fname)
            derives = _extract_rust_derives(node, root)
            if name:
                structs.append({"name": name, "fields": fields[:20], "derives": derives})

        elif node.type == "enum_item":
            name = _find(node, "type_identifier")
            variants = []
            body = _find_node(node, "enum_variant_list")
            if body:
                for v in _children_of_type(body, "enum_variant"):
                    vname = _find(v, "identifier")
                    if vname:
                        variants.append(vname)
            if name:
                enums.append({"name": name, "variants": variants[:20]})

        elif node.type == "function_item":
            name = _find(node, "identifier")
            is_async = any(c.type == "async" for c in node.children) if hasattr(node, 'children') else False
            # Check text for "async fn"
            fn_text = _text(node)[:50]
            is_async = "async fn" in fn_text
            params = _extract_rust_params(node)
            if name:
                functions.append({
                    "name": name,
                    "args": params,
                    "is_async": is_async,
                    "line": node.start_point[0] + 1,
                })

        elif node.type == "impl_item":
            type_name = _find(node, "type_identifier")
            if type_name:
                if type_name not in impl_methods:
                    impl_methods[type_name] = []
                body = _find_node(node, "declaration_list")
                if body:
                    for fn in _children_of_type(body, "function_item"):
                        fname = _find(fn, "identifier")
                        if fname:
                            impl_methods[type_name].append(fname)

    # Build classes from structs + impl methods
    classes = []
    for s in structs:
        methods = impl_methods.get(s["name"], [])
        cls = {
            "name": s["name"],
            "bases": s.get("derives", []),
            "fields": s["fields"],
            "methods": methods,
        }
        classes.append(cls)

    for e in enums:
        classes.append({
            "name": e["name"],
            "bases": ["enum"],
            "fields": e["variants"],
            "methods": impl_methods.get(e["name"], []),
        })

    # Detect Actix macro routes (#[get("/path")])
    for node in _walk(root):
        if node.type == "attribute_item":
            text = _text(node)
            for method in ("get", "post", "put", "delete", "patch"):
                if f"#{method}(" in text.lower() or f"#[{method}(" in text.lower():
                    path = ""
                    for child in _walk(node):
                        if child.type == "string_literal":
                            path = _text(child).strip('"')
                            break
                    handler = "?"
                    next_sib = node.next_named_sibling
                    if next_sib and next_sib.type == "function_item":
                        handler = _find(next_sib, "identifier") or "?"
                    routes.append({
                        "method": method.upper(),
                        "path": path,
                        "handler": handler,
                        "line": node.start_point[0] + 1,
                    })

    # Detect Axum builder routes (.route("/path", get(handler)))
    for node in _walk(root):
        if node.type == "call_expression":
            fn_node = _find_node(node, "field_expression")
            if fn_node and _find(fn_node, "field_identifier") == "route":
                args = _find_node(node, "arguments")
                if args:
                    arg_children = [c for c in args.children if c.type not in ("(", ")", ",")]
                    if len(arg_children) >= 2:
                        # First arg: path string
                        path_node = arg_children[0]
                        path = ""
                        if path_node.type == "string_literal":
                            path = _text(path_node).strip('"')
                        # Second arg: get(handler) or post(handler)
                        method_call = arg_children[1]
                        if method_call.type == "call_expression":
                            method_fn = _find(method_call, "identifier")
                            if method_fn and method_fn in ("get", "post", "put", "delete", "patch", "head"):
                                handler_args = _find_node(method_call, "arguments")
                                handler = "?"
                                if handler_args:
                                    handler = _find(handler_args, "identifier") or "?"
                                routes.append({
                                    "method": method_fn.upper(),
                                    "path": path,
                                    "handler": handler,
                                    "line": node.start_point[0] + 1,
                                })

    return {
        "imports": imports,
        "classes": classes,
        "functions": functions,
        "routes": routes,
    }


def _extract_rust_derives(struct_node, _root) -> list[str]:
    """Extract #[derive(...)] from the attribute before a struct."""
    derives = []
    prev = struct_node.prev_named_sibling
    if prev and prev.type == "attribute_item":
        text = _text(prev)
        if "derive" in text:
            # Parse derive(Serialize, Deserialize, ...)
            start = text.find("(")
            end = text.rfind(")")
            if start != -1 and end != -1:
                items = text[start+1:end].split(",")
                derives = [i.strip() for i in items if i.strip()]
    return derives


def _extract_rust_params(node) -> list[str]:
    params = []
    param_list = _find_node(node, "parameters")
    if param_list:
        for param in _children_of_type(param_list, "parameter"):
            name = _find(param, "identifier")
            if name and name != "self":
                params.append(name)
        # Also check for self_parameter
        for param in _children_of_type(param_list, "self_parameter"):
            pass  # skip self
    return params


# ── C# ────────────────────────────────────────────────────────────


def _extract_csharp(root) -> dict:
    imports = []
    classes = []
    routes = []

    for node in _walk(root):
        if node.type == "using_directive":
            name = _find(node, "qualified_name", "identifier")
            if name:
                imports.append(name)

        elif node.type == "class_declaration":
            cls = _extract_csharp_class(node)
            classes.append(cls)
            routes.extend(cls.pop("_routes", []))

    return {
        "imports": imports,
        "classes": classes,
        "functions": [],
        "routes": routes,
    }


def _extract_csharp_class(node) -> dict:
    name = _find(node, "identifier")
    bases = []
    fields = []
    methods = []
    routes = []

    # Base class / interfaces
    base_list = _find_node(node, "base_list")
    if base_list:
        for t in _walk(base_list):
            if t.type in ("identifier", "generic_name"):
                bases.append(_text(t))

    body = _find_node(node, "declaration_list")
    if body:
        for child in body.children:
            if child.type == "property_declaration":
                fname = _find(child, "identifier")
                if fname:
                    fields.append(fname)

            elif child.type == "field_declaration":
                for decl in _walk(child):
                    if decl.type == "variable_declarator":
                        fname = _find(decl, "identifier")
                        if fname:
                            fields.append(fname)

            elif child.type == "method_declaration":
                mname = _find(child, "identifier")
                if mname:
                    methods.append(mname)

                # Attributes are CHILDREN of method_declaration
                for attr_list in _children_of_type(child, "attribute_list"):
                    for attr in _children_of_type(attr_list, "attribute"):
                        attr_name = _find(attr, "identifier")
                        if attr_name in ("HttpGet", "HttpPost", "HttpPut", "HttpDelete", "HttpPatch"):
                            method = attr_name.replace("Http", "").upper()
                            route_path = ""
                            args = _find_node(attr, "attribute_argument_list")
                            if args:
                                for n in _walk(args):
                                    if n.type == "string_literal":
                                        route_path = _text(n).strip('"')
                                        break
                            routes.append({
                                "method": method,
                                "path": route_path or "/",
                                "handler": mname,
                                "line": child.start_point[0] + 1,
                            })

    return {
        "name": name or "?",
        "bases": bases,
        "fields": fields[:20],
        "methods": methods,
        "_routes": routes,
    }


# ── Ruby ──────────────────────────────────────────────────────────


def _extract_ruby(root) -> dict:
    imports = []
    classes = []
    functions = []
    routes = []

    for node in root.children:
        if node.type == "call":
            # require 'foo' or require_relative 'bar'
            method = _find(node, "identifier")
            if method in ("require", "require_relative", "gem"):
                args = _find_node(node, "argument_list")
                if args:
                    val = _find(args, "string", "string_content")
                    if val:
                        imports.append(val.strip("'\""))

        elif node.type == "class":
            cls = _extract_ruby_class(node)
            classes.append(cls)

        elif node.type == "method":
            name = _find(node, "identifier")
            if name:
                functions.append({
                    "name": name,
                    "args": [],
                    "is_async": False,
                    "line": node.start_point[0] + 1,
                })

    # Detect Rails/Sinatra routes
    for node in _walk(root):
        if node.type == "call":
            method_name = _find(node, "identifier")
            if method_name in ("get", "post", "put", "patch", "delete"):
                args = _find_node(node, "argument_list")
                if args:
                    path = _find(args, "string_content", "string", "simple_symbol")
                    if path:
                        path = path.strip("'\":").lstrip(":")
                        routes.append({
                            "method": method_name.upper(),
                            "path": path,
                            "handler": path.split("/")[-1] or path,
                            "line": node.start_point[0] + 1,
                        })
            elif method_name in ("resources", "resource"):
                args = _find_node(node, "argument_list")
                if args:
                    name = _find(args, "simple_symbol", "string_content", "string")
                    if name:
                        name = name.strip("'\":").lstrip(":")
                        routes.append({
                            "method": "RESOURCE",
                            "path": f"/{name}",
                            "handler": f"{name}_controller",
                            "line": node.start_point[0] + 1,
                        })

    return {
        "imports": imports,
        "classes": classes,
        "functions": functions,
        "routes": routes,
    }


def _extract_ruby_class(node) -> dict:
    name = _find(node, "constant", "scope_resolution")
    superclass = _find_node(node, "superclass")
    base = ""
    if superclass:
        base = _find(superclass, "constant", "scope_resolution") or ""

    methods = []
    body = _find_node(node, "body_statement")
    if body:
        for child in _walk(body):
            if child.type == "method":
                mname = _find(child, "identifier")
                if mname:
                    methods.append(mname)

    return {
        "name": name or "?",
        "bases": [base] if base else [],
        "fields": [],
        "methods": methods,
    }


# ── PHP ───────────────────────────────────────────────────────────


def _extract_php(root) -> dict:
    imports = []
    classes = []
    functions = []
    routes = []

    for node in _walk(root):
        if node.type == "namespace_use_declaration":
            for clause in _walk(node):
                if clause.type == "namespace_use_clause":
                    name = _find(clause, "qualified_name", "name")
                    if name:
                        imports.append(name)

        elif node.type == "class_declaration":
            cls = _extract_php_class(node)
            classes.append(cls)

        elif node.type == "function_definition":
            name = _find(node, "name")
            if name:
                functions.append({
                    "name": name,
                    "args": [],
                    "is_async": False,
                    "line": node.start_point[0] + 1,
                })

    # Detect Laravel routes: Route::get('path', 'Controller@method')
    def _php_string_arg(arg_node) -> str:
        """Extract string value from a PHP argument node (argument > string > string_value)."""
        string_node = _find_node(arg_node, "string", "encapsed_string")
        if string_node:
            sv = _find(string_node, "string_value")
            if sv:
                return sv
            return _text(string_node).strip("'\"")
        return ""

    for node in _walk(root):
        if node.type == "scoped_call_expression":
            scope_name = _find(node, "name")
            if scope_name == "Route":
                # Find the method name (get, post, etc.) - it's the second 'name' child
                names = [c for c in node.children if c.type == "name"]
                if len(names) >= 2:
                    method_name = _text(names[1])
                else:
                    method_name = ""

                if method_name in ("get", "post", "put", "patch", "delete", "match"):
                    args = _find_node(node, "arguments")
                    if args:
                        arg_children = [c for c in args.children if c.type == "argument"]
                        path = ""
                        handler = "?"
                        if len(arg_children) >= 1:
                            path = _php_string_arg(arg_children[0])
                        if len(arg_children) >= 2:
                            handler = _php_string_arg(arg_children[1]) or "?"
                        if path:
                            routes.append({
                                "method": method_name.upper(),
                                "path": path,
                                "handler": handler,
                                "line": node.start_point[0] + 1,
                            })
                elif method_name in ("resource", "apiResource"):
                    args = _find_node(node, "arguments")
                    if args:
                        arg_children = [c for c in args.children if c.type == "argument"]
                        if arg_children:
                            name = _php_string_arg(arg_children[0])
                            if name:
                                routes.append({
                                    "method": "RESOURCE",
                                    "path": name,
                                    "handler": name,
                                    "line": node.start_point[0] + 1,
                                })

    return {
        "imports": imports,
        "classes": classes,
        "functions": functions,
        "routes": routes,
    }


def _extract_php_class(node) -> dict:
    name = _find(node, "name")
    bases = []

    base = _find_node(node, "base_clause")
    if base:
        bname = _find(base, "qualified_name", "name")
        if bname:
            bases.append(bname)

    ifaces = _find_node(node, "class_interface_clause")
    if ifaces:
        for n in _walk(ifaces):
            if n.type in ("qualified_name", "name"):
                bases.append(_text(n))

    methods = []
    fields = []
    body = _find_node(node, "declaration_list")
    if body:
        for child in body.children:
            if child.type == "method_declaration":
                mname = _find(child, "name")
                if mname:
                    methods.append(mname)
            elif child.type == "property_declaration":
                for decl in _walk(child):
                    if decl.type == "property_element":
                        fname = _find(decl, "variable_name")
                        if fname:
                            fields.append(fname)

    return {
        "name": name or "?",
        "bases": bases,
        "fields": fields[:20],
        "methods": methods,
    }
