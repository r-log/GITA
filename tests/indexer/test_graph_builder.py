"""Tests for the deterministic graph builder — node and edge construction."""

from src.indexer.parsers import FileIndex
from src.indexer.graph_builder import (
    _build_nodes_for_file,
    _build_edges_for_file,
    _build_module_lookup,
    _resolve_relative_import,
)


REPO_ID = 1


# ── Node Construction ─────────────────────────────────────────────


class TestBuildNodesForFile:
    def test_file_node_always_created(self):
        fi = FileIndex(
            file_path="src/main.py",
            language="python",
            size_bytes=100,
            line_count=10,
            structure={},
            content_hash="abc123",
        )
        nodes = _build_nodes_for_file(REPO_ID, fi)
        assert len(nodes) == 1
        assert nodes[0].node_type == "file"
        assert nodes[0].qualified_name == "src/main.py"
        assert nodes[0].name == "main.py"

    def test_python_classes_and_methods(self):
        fi = FileIndex(
            file_path="src/models/user.py",
            language="python",
            size_bytes=500,
            line_count=50,
            structure={
                "imports": ["sqlalchemy"],
                "classes": [
                    {
                        "name": "User",
                        "bases": ["Base"],
                        "decorators": [],
                        "methods": ["__init__", "to_dict"],
                        "method_details": [
                            {"name": "__init__", "args": ["name", "email"], "decorators": [], "line": 5},
                            {"name": "to_dict", "args": [], "decorators": [], "line": 10},
                        ],
                        "fields": ["id", "name", "email"],
                        "line": 3,
                    }
                ],
                "functions": [],
            },
            content_hash="abc123",
        )
        nodes = _build_nodes_for_file(REPO_ID, fi)

        types = {n.node_type for n in nodes}
        assert "file" in types
        assert "class" in types
        assert "method" in types

        # File + 1 class + 2 methods = 4 nodes
        assert len(nodes) == 4

        class_node = [n for n in nodes if n.node_type == "class"][0]
        assert class_node.qualified_name == "src/models/user.py::User"
        assert class_node.name == "User"
        assert class_node.line_number == 3
        assert class_node.extra["bases"] == ["Base"]

        methods = [n for n in nodes if n.node_type == "method"]
        method_names = {m.name for m in methods}
        assert method_names == {"__init__", "to_dict"}

    def test_python_functions(self):
        fi = FileIndex(
            file_path="src/utils.py",
            language="python",
            size_bytes=200,
            line_count=20,
            structure={
                "functions": [
                    {"name": "helper", "args": ["x", "y"], "decorators": ["staticmethod"], "is_async": False, "line": 1},
                    {"name": "async_helper", "args": [], "decorators": [], "is_async": True, "line": 10},
                ],
            },
            content_hash="abc123",
        )
        nodes = _build_nodes_for_file(REPO_ID, fi)

        funcs = [n for n in nodes if n.node_type == "function"]
        assert len(funcs) == 2
        assert funcs[0].qualified_name == "src/utils.py::helper"
        assert funcs[0].extra["is_async"] is False
        assert funcs[1].extra["is_async"] is True

    def test_routes(self):
        fi = FileIndex(
            file_path="src/api/routes.py",
            language="python",
            size_bytes=100,
            line_count=10,
            structure={
                "routes": [
                    {"method": "GET", "path": "/users", "handler": "list_users", "line": 5},
                    {"method": "POST", "path": "/users", "handler": "create_user", "line": 15},
                ],
            },
            content_hash="abc123",
        )
        nodes = _build_nodes_for_file(REPO_ID, fi)

        routes = [n for n in nodes if n.node_type == "route"]
        assert len(routes) == 2
        assert routes[0].name == "GET /users"
        assert routes[0].extra["handler"] == "list_users"

    def test_js_components(self):
        fi = FileIndex(
            file_path="src/components/Dashboard.tsx",
            language="typescript",
            size_bytes=300,
            line_count=30,
            structure={
                "components": ["Dashboard", "Header"],
                "functions": [{"name": "useData", "line": 5}],
            },
            content_hash="abc123",
        )
        nodes = _build_nodes_for_file(REPO_ID, fi)

        # File + 2 components + 1 function = 4 nodes
        assert len(nodes) == 4
        comps = [n for n in nodes if n.extra and n.extra.get("is_component")]
        assert len(comps) == 2

    def test_empty_structure(self):
        fi = FileIndex(
            file_path="README.md",
            language="markdown",
            size_bytes=50,
            line_count=5,
            structure={},
            content_hash="abc123",
        )
        nodes = _build_nodes_for_file(REPO_ID, fi)
        assert len(nodes) == 1
        assert nodes[0].node_type == "file"

    def test_class_without_method_details_fallback(self):
        """When method_details is missing, fall back to methods list."""
        fi = FileIndex(
            file_path="src/model.py",
            language="python",
            size_bytes=100,
            line_count=10,
            structure={
                "classes": [
                    {
                        "name": "Foo",
                        "bases": [],
                        "decorators": [],
                        "methods": ["bar", "baz"],
                        "fields": [],
                        "line": 1,
                    }
                ],
            },
            content_hash="abc123",
        )
        nodes = _build_nodes_for_file(REPO_ID, fi)
        methods = [n for n in nodes if n.node_type == "method"]
        assert len(methods) == 2
        assert {m.name for m in methods} == {"bar", "baz"}


# ── Edge Construction ─────────────────────────────────────────────


class TestBuildEdgesForFile:
    def _make_lookup(self, nodes):
        return {n.qualified_name: idx + 1 for idx, n in enumerate(nodes)}

    def test_import_edges_python(self):
        """Python imports resolve to file nodes via module lookup."""
        fi = FileIndex(
            file_path="src/api/routes.py",
            language="python",
            size_bytes=100,
            line_count=10,
            structure={"imports": ["src.models.user"]},
            content_hash="abc123",
        )
        # Simulate: the source file node exists and the target file node exists
        node_lookup = {
            "src/api/routes.py": 1,
            "src/models/user.py": 2,
            "src/models/user.py::User": 3,
        }
        module_lookup = {"src.models.user": 2}
        file_path_to_node = {"src/api/routes.py": 1, "src/models/user.py": 2}

        edges = _build_edges_for_file(REPO_ID, fi, node_lookup, module_lookup, file_path_to_node)

        import_edges = [e for e in edges if e.edge_type == "imports"]
        assert len(import_edges) == 1
        assert import_edges[0].source_node_id == 1  # routes.py
        assert import_edges[0].target_node_id == 2  # user.py

    def test_defines_edges(self):
        """File should have defines edges to its classes and functions."""
        fi = FileIndex(
            file_path="src/utils.py",
            language="python",
            size_bytes=100,
            line_count=10,
            structure={
                "classes": [{"name": "Helper", "bases": [], "decorators": [], "methods": [], "fields": [], "line": 1}],
                "functions": [{"name": "do_stuff", "args": [], "decorators": [], "is_async": False, "line": 10}],
            },
            content_hash="abc123",
        )
        node_lookup = {
            "src/utils.py": 1,
            "src/utils.py::Helper": 2,
            "src/utils.py::do_stuff": 3,
        }

        edges = _build_edges_for_file(REPO_ID, fi, node_lookup, {}, {"src/utils.py": 1})

        defines = [e for e in edges if e.edge_type == "defines"]
        assert len(defines) == 2
        targets = {e.target_node_id for e in defines}
        assert targets == {2, 3}

    def test_inheritance_edges(self):
        fi = FileIndex(
            file_path="src/models/admin.py",
            language="python",
            size_bytes=100,
            line_count=10,
            structure={
                "classes": [
                    {"name": "Admin", "bases": ["User"], "decorators": [], "methods": [], "fields": [], "line": 1}
                ],
            },
            content_hash="abc123",
        )
        node_lookup = {
            "src/models/admin.py": 1,
            "src/models/admin.py::Admin": 2,
            "src/models/user.py::User": 3,
        }

        edges = _build_edges_for_file(REPO_ID, fi, node_lookup, {}, {"src/models/admin.py": 1})

        inherits = [e for e in edges if e.edge_type == "inherits"]
        assert len(inherits) == 1
        assert inherits[0].source_node_id == 2  # Admin
        assert inherits[0].target_node_id == 3  # User

    def test_no_edges_for_missing_file_node(self):
        fi = FileIndex(
            file_path="missing.py",
            language="python",
            size_bytes=100,
            line_count=10,
            structure={"imports": ["os"]},
            content_hash="abc123",
        )
        edges = _build_edges_for_file(REPO_ID, fi, {}, {}, {})
        assert edges == []


# ── Module Lookup ─────────────────────────────────────────────────


class TestModuleLookup:
    def test_python_module_paths(self):
        files = [
            FileIndex(file_path="src/models/user.py", language="python", size_bytes=0, line_count=0, content_hash=""),
            FileIndex(file_path="src/api/__init__.py", language="python", size_bytes=0, line_count=0, content_hash=""),
            FileIndex(file_path="app.js", language="javascript", size_bytes=0, line_count=0, content_hash=""),
        ]
        node_lookup = {
            "src/models/user.py": 1,
            "src/api/__init__.py": 2,
            "app.js": 3,
        }

        lookup = _build_module_lookup(files, node_lookup)

        assert lookup["src.models.user"] == 1
        assert lookup["user"] == 1  # short form
        assert lookup["src.api"] == 2  # __init__ stripped
        assert "app" not in lookup  # JS files not in module lookup


# ── Relative Import Resolution ────────────────────────────────────


class TestResolveRelativeImport:
    def test_same_directory(self):
        candidates = _resolve_relative_import("./utils", "src/components/App.tsx")
        assert "src/components/utils" in candidates
        assert "src/components/utils.ts" in candidates
        assert "src/components/utils.tsx" in candidates

    def test_parent_directory(self):
        candidates = _resolve_relative_import("../helpers", "src/components/App.tsx")
        assert "src/helpers" in candidates

    def test_nested(self):
        candidates = _resolve_relative_import("./sub/module", "src/lib/index.ts")
        assert "src/lib/sub/module" in candidates
        assert "src/lib/sub/module.ts" in candidates

    def test_index_files(self):
        candidates = _resolve_relative_import("./Button", "src/components/App.tsx")
        assert "src/components/Button/index.ts" in candidates
        assert "src/components/Button/index.tsx" in candidates


# ── Import Resolution ────────────────────────────────────────────

from src.indexer.graph_builder import (
    _resolve_import, _resolve_symbol,
    _build_module_lookup_from_nodes,
    build_graph_for_repo, update_graph_for_files,
)
from unittest.mock import AsyncMock, MagicMock, patch


class TestResolveImport:
    def test_direct_module_lookup(self):
        module_lookup = {"src.models.issue": 10}
        fi = FileIndex(file_path="src/main.py", language="python",
                       size_bytes=1, line_count=1, structure={}, content_hash="a")
        result = _resolve_import("src.models.issue", fi, module_lookup, {})
        assert result == 10

    def test_dotted_path_to_file(self):
        file_path_to_node = {"src/models/issue.py": 20}
        fi = FileIndex(file_path="src/main.py", language="python",
                       size_bytes=1, line_count=1, structure={}, content_hash="a")
        result = _resolve_import("src.models.issue", fi, {}, file_path_to_node)
        assert result == 20

    def test_dotted_path_to_init(self):
        file_path_to_node = {"src/models/__init__.py": 30}
        fi = FileIndex(file_path="src/main.py", language="python",
                       size_bytes=1, line_count=1, structure={}, content_hash="a")
        result = _resolve_import("src.models", fi, {}, file_path_to_node)
        assert result == 30

    def test_relative_import_js(self):
        file_path_to_node = {"src/utils/helper.ts": 40}
        fi = FileIndex(file_path="src/main.ts", language="typescript",
                       size_bytes=1, line_count=1, structure={}, content_hash="a")
        result = _resolve_import("./utils/helper", fi, {}, file_path_to_node)
        assert result == 40

    def test_direct_file_path(self):
        file_path_to_node = {"lib/util.py": 50}
        fi = FileIndex(file_path="src/main.py", language="python",
                       size_bytes=1, line_count=1, structure={}, content_hash="a")
        result = _resolve_import("lib/util.py", fi, {}, file_path_to_node)
        assert result == 50

    def test_unresolved_returns_none(self):
        fi = FileIndex(file_path="src/main.py", language="python",
                       size_bytes=1, line_count=1, structure={}, content_hash="a")
        result = _resolve_import("external_lib", fi, {}, {})
        assert result is None


class TestResolveSymbol:
    def test_same_file_qualified_name(self):
        fi = FileIndex(file_path="src/main.py", language="python",
                       size_bytes=1, line_count=1, structure={}, content_hash="a")
        node_lookup = {"src/main.py::MyClass": 10}
        result = _resolve_symbol("MyClass", fi, node_lookup, {})
        assert result == 10

    def test_cross_file_match(self):
        fi = FileIndex(file_path="src/main.py", language="python",
                       size_bytes=1, line_count=1, structure={}, content_hash="a")
        node_lookup = {"src/models/base.py::BaseModel": 20}
        result = _resolve_symbol("BaseModel", fi, node_lookup, {})
        assert result == 20

    def test_module_lookup_fallback(self):
        fi = FileIndex(file_path="src/main.py", language="python",
                       size_bytes=1, line_count=1, structure={}, content_hash="a")
        result = _resolve_symbol("SomeModule", fi, {}, {"SomeModule": 30})
        assert result == 30

    def test_unresolved_returns_none(self):
        fi = FileIndex(file_path="src/main.py", language="python",
                       size_bytes=1, line_count=1, structure={}, content_hash="a")
        result = _resolve_symbol("Unknown", fi, {}, {})
        assert result is None


class TestBuildModuleLookupFromNodes:
    def test_python_files(self):
        node1 = MagicMock()
        node1.node_type = "file"
        node1.file_path = "src/models/issue.py"
        node1.language = "python"
        node1.id = 10

        node2 = MagicMock()
        node2.node_type = "file"
        node2.file_path = "src/__init__.py"
        node2.language = "python"
        node2.id = 20

        lookup = _build_module_lookup_from_nodes([node1, node2])
        assert "src.models.issue" in lookup
        assert lookup["src.models.issue"] == 10
        assert "src" in lookup  # __init__ package

    def test_non_file_nodes_skipped(self):
        node = MagicMock()
        node.node_type = "class"
        node.file_path = "src/main.py"
        node.language = "python"
        node.id = 10

        lookup = _build_module_lookup_from_nodes([node])
        assert len(lookup) == 0

    def test_non_python_skipped(self):
        node = MagicMock()
        node.node_type = "file"
        node.file_path = "src/main.ts"
        node.language = "typescript"
        node.id = 10

        lookup = _build_module_lookup_from_nodes([node])
        assert len(lookup) == 0


# ── Async Integration (mocked DB) ───────────────────────────────


def _mock_session():
    session = AsyncMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.add = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return session, ctx


class TestBuildGraphForRepo:
    @patch("src.indexer.graph_builder.async_session")
    async def test_full_build(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        # Mock FileMapping query for _migrate_file_mappings
        session.execute.return_value = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        )

        fi = FileIndex(
            file_path="src/main.py", language="python",
            size_bytes=50, line_count=5, content_hash="abc",
            structure={"functions": [{"name": "main", "line": 1}]},
        )
        result = await build_graph_for_repo(REPO_ID, [fi])
        assert result["nodes_created"] >= 2  # file + function
        assert result["edges_created"] >= 0

    @patch("src.indexer.graph_builder.async_session")
    async def test_empty_files(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        )

        result = await build_graph_for_repo(REPO_ID, [])
        assert result["nodes_created"] == 0


class TestUpdateGraphForFiles:
    @patch("src.indexer.graph_builder.async_session")
    async def test_no_changes(self, mock_factory):
        result = await update_graph_for_files(REPO_ID, [], set())
        assert result["nodes_updated"] == 0
        assert result["edges_updated"] == 0

    @patch("src.indexer.graph_builder.async_session")
    async def test_with_changed_files(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        # Mock old node IDs query
        session.execute.return_value = MagicMock(
            all=MagicMock(return_value=[]),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
        )

        fi = FileIndex(
            file_path="src/updated.py", language="python",
            size_bytes=50, line_count=5, content_hash="def",
            structure={"functions": [{"name": "updated_fn", "line": 1}]},
        )
        result = await update_graph_for_files(REPO_ID, [fi], set())
        assert result["nodes_updated"] >= 1

    @patch("src.indexer.graph_builder.async_session")
    async def test_with_removed_files(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(
            all=MagicMock(return_value=[(1,)]),  # one old node ID
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
        )

        result = await update_graph_for_files(REPO_ID, [], {"src/deleted.py"})
        assert result["nodes_updated"] == 0
