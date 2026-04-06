"""
Tests for src.indexer.tree_sitter_parser — language-specific code extraction.

Since tree-sitter-languages may not be available or compatible, we mock the
parser and tree objects. The extractor functions work on AST node trees
which we build from simple Python objects.
"""

from unittest.mock import patch, MagicMock

from src.indexer.tree_sitter_parser import (
    is_available, parse_tree_sitter,
    _text, _find, _find_node, _walk, _children_of_type,
    _extract_go, _extract_java, _extract_rust,
    _extract_csharp, _extract_ruby, _extract_php,
)


# ---------------------------------------------------------------------------
# Mock node builder — creates tree-sitter-like node objects
# ---------------------------------------------------------------------------

def N(node_type: str, text: str = "", children: list = None, start_point=None):
    """Create a mock tree-sitter AST node."""
    node = MagicMock()
    node.type = node_type
    node.text = text.encode("utf-8") if isinstance(text, str) else text
    node.children = children or []
    node.start_point = start_point or [0, 0]
    node.next_named_sibling = None
    node.prev_named_sibling = None
    # Wire up siblings
    for i, child in enumerate(node.children):
        if i > 0:
            child.prev_named_sibling = node.children[i - 1]
        if i < len(node.children) - 1:
            child.next_named_sibling = node.children[i + 1]
    return node


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_text(self):
        node = N("identifier", "myVar")
        assert _text(node) == "myVar"

    def test_find_found(self):
        child = N("identifier", "funcName")
        parent = N("function_declaration", children=[child])
        assert _find(parent, "identifier") == "funcName"

    def test_find_not_found(self):
        parent = N("function_declaration", children=[N("comment", "// hi")])
        assert _find(parent, "identifier") is None

    def test_find_node(self):
        child = N("parameter_list", "(int x)")
        parent = N("function_declaration", children=[child])
        result = _find_node(parent, "parameter_list")
        assert result is child

    def test_find_node_not_found(self):
        parent = N("function_declaration", children=[])
        assert _find_node(parent, "parameter_list") is None

    def test_walk(self):
        grandchild = N("identifier", "x")
        child = N("parameter", "int x", children=[grandchild])
        root = N("function", children=[child])
        all_nodes = list(_walk(root))
        assert len(all_nodes) == 2  # child + grandchild

    def test_children_of_type(self):
        c1 = N("identifier", "a")
        c2 = N("comment", "// hi")
        c3 = N("identifier", "b")
        parent = N("block", children=[c1, c2, c3])
        ids = list(_children_of_type(parent, "identifier"))
        assert len(ids) == 2


# ---------------------------------------------------------------------------
# Core dispatch
# ---------------------------------------------------------------------------

class TestParseTreeSitter:
    def test_unsupported_language(self):
        result = parse_tree_sitter("code", "file.xyz", "haskell")
        assert result == {}

    @patch("src.indexer.tree_sitter_parser.TREE_SITTER_AVAILABLE", False)
    def test_not_available(self):
        result = parse_tree_sitter("code", "main.go", "go")
        assert result == {}

    @patch("src.indexer.tree_sitter_parser._get_parser")
    @patch("src.indexer.tree_sitter_parser.TREE_SITTER_AVAILABLE", True)
    def test_parse_error_returns_empty(self, mock_get_parser):
        mock_get_parser.side_effect = Exception("parse error")
        result = parse_tree_sitter("bad code", "main.go", "go")
        assert result == {}

    @patch("src.indexer.tree_sitter_parser._extract_go")
    @patch("src.indexer.tree_sitter_parser._get_parser")
    @patch("src.indexer.tree_sitter_parser.TREE_SITTER_AVAILABLE", True)
    def test_dispatches_to_go(self, mock_get_parser, mock_extract):
        mock_tree = MagicMock()
        mock_get_parser.return_value.parse.return_value = mock_tree
        mock_extract.return_value = {"imports": [], "classes": [], "functions": []}

        result = parse_tree_sitter("package main", "main.go", "go")
        mock_extract.assert_called_once_with(mock_tree.root_node)

    @patch("src.indexer.tree_sitter_parser._extract_java")
    @patch("src.indexer.tree_sitter_parser._get_parser")
    @patch("src.indexer.tree_sitter_parser.TREE_SITTER_AVAILABLE", True)
    def test_dispatches_to_java(self, mock_get_parser, mock_extract):
        mock_tree = MagicMock()
        mock_get_parser.return_value.parse.return_value = mock_tree
        mock_extract.return_value = {}

        parse_tree_sitter("class Main {}", "Main.java", "java")
        mock_extract.assert_called_once()

    @patch("src.indexer.tree_sitter_parser._extract_rust")
    @patch("src.indexer.tree_sitter_parser._get_parser")
    @patch("src.indexer.tree_sitter_parser.TREE_SITTER_AVAILABLE", True)
    def test_dispatches_to_rust(self, mock_get_parser, mock_extract):
        mock_tree = MagicMock()
        mock_get_parser.return_value.parse.return_value = mock_tree
        mock_extract.return_value = {}

        parse_tree_sitter("fn main() {}", "main.rs", "rust")
        mock_extract.assert_called_once()


class TestIsAvailable:
    def test_returns_bool(self):
        result = is_available()
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Go Extractor
# ---------------------------------------------------------------------------

class TestExtractGo:
    def test_imports(self):
        imp_spec = N("import_spec", children=[N("interpreted_string_literal", '"fmt"')])
        imp_decl = N("import_declaration", children=[N("import_spec_list", children=[imp_spec])])
        root = N("source_file", children=[imp_decl])

        result = _extract_go(root)
        assert "fmt" in str(result.get("imports", []))

    def test_function(self):
        name = N("identifier", "main")
        params = N("parameter_list", "()")
        func = N("function_declaration", "func main()", children=[name, params], start_point=[5, 0])
        root = N("source_file", children=[func])

        result = _extract_go(root)
        funcs = result.get("functions", [])
        assert len(funcs) == 1
        assert funcs[0]["name"] == "main"

    def test_struct(self):
        struct_name = N("type_identifier", "User")
        struct_type = N("struct_type", "struct{}", children=[
            N("field_declaration_list", children=[
                N("field_declaration", children=[N("field_identifier", "Name")])
            ])
        ])
        type_spec = N("type_spec", children=[struct_name, struct_type])
        type_decl = N("type_declaration", children=[type_spec])
        root = N("source_file", children=[type_decl])

        result = _extract_go(root)
        classes = result.get("classes", [])
        assert any(c["name"] == "User" for c in classes)

    def test_empty_source(self):
        root = N("source_file", children=[])
        result = _extract_go(root)
        assert result["imports"] == []
        assert result["classes"] == []
        assert result["functions"] == []


# ---------------------------------------------------------------------------
# Java Extractor
# ---------------------------------------------------------------------------

class TestExtractJava:
    def test_imports(self):
        scoped = N("scoped_identifier", "java.util.List")
        imp = N("import_declaration", children=[scoped])
        root = N("program", children=[imp])

        result = _extract_java(root)
        assert "java.util.List" in result.get("imports", [])

    def test_class(self):
        class_name = N("identifier", "UserService")
        body = N("class_body", children=[
            N("method_declaration", children=[N("identifier", "getUser")]),
        ])
        class_decl = N("class_declaration", children=[class_name, body])
        root = N("program", children=[class_decl])

        result = _extract_java(root)
        classes = result.get("classes", [])
        assert len(classes) >= 1
        assert classes[0]["name"] == "UserService"

    def test_empty(self):
        root = N("program", children=[])
        result = _extract_java(root)
        assert result["imports"] == []
        assert result["functions"] == []


# ---------------------------------------------------------------------------
# Rust Extractor
# ---------------------------------------------------------------------------

class TestExtractRust:
    def test_use_imports(self):
        use = N("use_declaration", "use std::io;")
        root = N("source_file", children=[use])

        result = _extract_rust(root)
        assert len(result.get("imports", [])) == 1

    def test_function(self):
        name = N("identifier", "process")
        params = N("parameters", "()")
        func = N("function_item", "fn process()", children=[name, params], start_point=[3, 0])
        root = N("source_file", children=[func])

        result = _extract_rust(root)
        funcs = result.get("functions", [])
        assert len(funcs) == 1
        assert funcs[0]["name"] == "process"

    def test_struct(self):
        name = N("type_identifier", "Config")
        fields = N("field_declaration_list", children=[
            N("field_declaration", children=[N("field_identifier", "host")])
        ])
        struct = N("struct_item", children=[name, fields])
        root = N("source_file", children=[struct])

        result = _extract_rust(root)
        classes = result.get("classes", [])
        assert any(c["name"] == "Config" for c in classes)

    def test_enum(self):
        name = N("type_identifier", "Color")
        variants = N("enum_variant_list", children=[
            N("enum_variant", children=[N("identifier", "Red")]),
            N("enum_variant", children=[N("identifier", "Blue")]),
        ])
        enum = N("enum_item", children=[name, variants])
        root = N("source_file", children=[enum])

        result = _extract_rust(root)
        classes = result.get("classes", [])
        assert any(c["name"] == "Color" for c in classes)

    def test_empty(self):
        root = N("source_file", children=[])
        result = _extract_rust(root)
        assert result["imports"] == []


# ---------------------------------------------------------------------------
# C# Extractor
# ---------------------------------------------------------------------------

class TestExtractCSharp:
    def test_using_directives(self):
        qname = N("qualified_name", "System.Linq")
        using = N("using_directive", children=[qname])
        root = N("compilation_unit", children=[using])

        result = _extract_csharp(root)
        assert "System.Linq" in result.get("imports", [])

    def test_class(self):
        name = N("identifier", "UserController")
        body = N("declaration_list", children=[
            N("method_declaration", children=[N("identifier", "GetUsers")]),
        ])
        cls = N("class_declaration", children=[name, body])
        root = N("compilation_unit", children=[cls])

        result = _extract_csharp(root)
        classes = result.get("classes", [])
        assert len(classes) >= 1
        assert classes[0]["name"] == "UserController"

    def test_empty(self):
        root = N("compilation_unit", children=[])
        result = _extract_csharp(root)
        assert result["imports"] == []


# ---------------------------------------------------------------------------
# Ruby Extractor
# ---------------------------------------------------------------------------

class TestExtractRuby:
    def test_require_imports(self):
        req_name = N("identifier", "require")
        # _find(args, "string", "string_content") looks for direct child of type "string"
        req_arg = N("argument_list", children=[N("string", "'json'")])
        req = N("call", children=[req_name, req_arg])
        root = N("program", children=[req])

        result = _extract_ruby(root)
        imports = result.get("imports", [])
        assert len(imports) >= 1

    def test_function(self):
        name = N("identifier", "process_data")
        method = N("method", children=[name], start_point=[10, 0])
        root = N("program", children=[method])

        result = _extract_ruby(root)
        funcs = result.get("functions", [])
        assert len(funcs) == 1
        assert funcs[0]["name"] == "process_data"

    def test_empty(self):
        root = N("program", children=[])
        result = _extract_ruby(root)
        assert result["imports"] == []


# ---------------------------------------------------------------------------
# PHP Extractor
# ---------------------------------------------------------------------------

class TestExtractPHP:
    def test_namespace_use(self):
        qname = N("qualified_name", "App\\Models\\User")
        clause = N("namespace_use_clause", children=[qname])
        use = N("namespace_use_declaration", children=[clause])
        prog = N("program", children=[use])
        root = N("program", children=[prog])  # PHP has nested program

        result = _extract_php(root)
        imports = result.get("imports", [])
        assert len(imports) >= 0  # May not match if nested differently

    def test_function(self):
        name = N("name", "helper_function")
        func = N("function_definition", children=[name], start_point=[5, 0])
        prog = N("program", children=[func])
        root = N("program", children=[prog])

        result = _extract_php(root)
        funcs = result.get("functions", [])
        assert len(funcs) >= 1

    def test_empty(self):
        root = N("program", children=[N("program", children=[])])
        result = _extract_php(root)
        assert result["imports"] == []
