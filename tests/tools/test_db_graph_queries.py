"""Tests for src.tools.db.graph_queries — graph traversal queries."""

from unittest.mock import AsyncMock, MagicMock, patch

from src.tools.db.graph_queries import (
    _get_blast_radius, _get_file_ownership, _get_focused_code_map,
    _get_file_dependents, _get_file_dependencies, _get_milestone_file_coverage,
    _get_symbol_usages,
    make_get_blast_radius, make_get_file_dependents, make_get_file_dependencies,
    make_get_file_ownership, make_get_symbol_usages, make_get_milestone_file_coverage,
    make_get_focused_code_map,
)


def _mock_session_with_results(*results):
    """Create a mock session that returns different results per execute() call."""
    session = AsyncMock()
    call_count = [0]

    async def mock_execute(stmt, params=None):
        idx = call_count[0]
        call_count[0] += 1
        if idx < len(results):
            return results[idx]
        return MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
            all=MagicMock(return_value=[]),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
        )

    session.execute = mock_execute
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# _get_file_dependents
# ---------------------------------------------------------------------------

class TestGetFileDependents:
    @patch("src.tools.db.graph_queries.async_session")
    async def test_with_dependents(self, mock_factory):
        # First query: find target node ID
        q1 = MagicMock(scalar_one_or_none=MagicMock(return_value=10))
        # Second query: find dependent files
        q2 = MagicMock(all=MagicMock(return_value=[("src/main.py", "python")]))
        mock_factory.return_value = _mock_session_with_results(q1, q2)

        result = await _get_file_dependents(42, "src/utils.py")
        assert result.success is True
        assert result.data["count"] == 1
        assert result.data["dependents"][0]["file_path"] == "src/main.py"

    @patch("src.tools.db.graph_queries.async_session")
    async def test_file_not_found(self, mock_factory):
        q1 = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        mock_factory.return_value = _mock_session_with_results(q1)

        result = await _get_file_dependents(42, "nonexistent.py")
        assert result.success is True
        assert result.data["count"] == 0

    @patch("src.tools.db.graph_queries.async_session")
    async def test_error(self, mock_factory):
        mock_factory.side_effect = Exception("db error")
        result = await _get_file_dependents(42, "src/main.py")
        assert result.success is False


# ---------------------------------------------------------------------------
# _get_file_dependencies
# ---------------------------------------------------------------------------

class TestGetFileDependencies:
    @patch("src.tools.db.graph_queries.async_session")
    async def test_with_dependencies(self, mock_factory):
        q1 = MagicMock(scalar_one_or_none=MagicMock(return_value=10))
        q2 = MagicMock(all=MagicMock(return_value=[("src/models/base.py", "python")]))
        mock_factory.return_value = _mock_session_with_results(q1, q2)

        result = await _get_file_dependencies(42, "src/main.py")
        assert result.success is True
        assert result.data["count"] == 1

    @patch("src.tools.db.graph_queries.async_session")
    async def test_file_not_found(self, mock_factory):
        q1 = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        mock_factory.return_value = _mock_session_with_results(q1)

        result = await _get_file_dependencies(42, "nonexistent.py")
        assert result.success is True
        assert result.data["count"] == 0

    @patch("src.tools.db.graph_queries.async_session")
    async def test_error(self, mock_factory):
        mock_factory.side_effect = Exception("db error")
        result = await _get_file_dependencies(42, "src/main.py")
        assert result.success is False


# ---------------------------------------------------------------------------
# _get_blast_radius
# ---------------------------------------------------------------------------

class TestGetBlastRadius:
    async def test_empty_file_paths(self):
        result = await _get_blast_radius(42, [], depth=2)
        assert result.success is True
        assert result.data["affected_files"] == []

    @patch("src.tools.db.graph_queries.async_session")
    async def test_with_files(self, mock_factory):
        # CTE query result
        q1 = MagicMock(all=MagicMock(return_value=[
            ("src/main.py", 0), ("src/utils.py", 1),
        ]))
        # Node IDs query
        q2 = MagicMock(all=MagicMock(return_value=[(1, "src/main.py"), (2, "src/utils.py")]))
        # Issue edges
        q3 = MagicMock(all=MagicMock(return_value=[(5, 0.9, "src/main.py")]))
        # Milestone edges
        q4 = MagicMock(all=MagicMock(return_value=[]))

        mock_factory.return_value = _mock_session_with_results(q1, q2, q3, q4)

        result = await _get_blast_radius(42, ["src/main.py"], depth=2)
        assert result.success is True
        assert result.data["total_affected"] == 2
        assert len(result.data["affected_issues"]) == 1

    @patch("src.tools.db.graph_queries.async_session")
    async def test_error(self, mock_factory):
        mock_factory.side_effect = Exception("db error")
        result = await _get_blast_radius(42, ["src/main.py"])
        assert result.success is False


# ---------------------------------------------------------------------------
# _get_file_ownership
# ---------------------------------------------------------------------------

class TestGetFileOwnership:
    @patch("src.tools.db.graph_queries.async_session")
    async def test_with_ownership(self, mock_factory):
        # File node IDs query: (id, file_path)
        q1 = MagicMock(all=MagicMock(return_value=[(1, "src/main.py")]))
        # Entity edges: (file_path, edge_type, entity_type, entity_id, confidence)
        q2 = MagicMock(all=MagicMock(return_value=[
            ("src/main.py", "belongs_to_issue", "issue", 5, 0.9),
        ]))

        mock_factory.return_value = _mock_session_with_results(q1, q2)

        result = await _get_file_ownership(42, ["src/main.py"])
        assert result.success is True
        assert "files" in result.data
        assert len(result.data["files"]) == 1

    @patch("src.tools.db.graph_queries.async_session")
    async def test_empty_paths(self, mock_factory):
        q1 = MagicMock(all=MagicMock(return_value=[]))
        mock_factory.return_value = _mock_session_with_results(q1)

        result = await _get_file_ownership(42, [])
        assert result.success is True

    @patch("src.tools.db.graph_queries.async_session")
    async def test_error(self, mock_factory):
        mock_factory.side_effect = Exception("db error")
        result = await _get_file_ownership(42, ["src/main.py"])
        assert result.success is False


# ---------------------------------------------------------------------------
# _get_symbol_usages
# ---------------------------------------------------------------------------

class TestGetSymbolUsages:
    @patch("src.tools.db.graph_queries.async_session")
    async def test_found(self, mock_factory):
        # Symbol node objects
        sym_node = MagicMock()
        sym_node.id = 10
        sym_node.qualified_name = "src/models/user.py::User"
        sym_node.file_path = "src/models/user.py"
        sym_node.node_type = "class"

        # q1: symbol_result.scalars().all() returns node objects
        q1 = MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[sym_node]))))
        # q2: file_node_result.all() returns (id,) tuples
        q2 = MagicMock(all=MagicMock(return_value=[(20,)]))
        # q3: import_result.all() returns (file_path, language) tuples
        q3 = MagicMock(all=MagicMock(return_value=[("src/main.py", "python")]))
        # q4: inherits_result.all()
        q4 = MagicMock(all=MagicMock(return_value=[]))

        mock_factory.return_value = _mock_session_with_results(q1, q2, q3, q4)

        result = await _get_symbol_usages(42, "User")
        assert result.success is True
        assert result.data["usage_count"] == 1
        assert len(result.data["definitions"]) == 1

    @patch("src.tools.db.graph_queries.async_session")
    async def test_not_found(self, mock_factory):
        q1 = MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
        mock_factory.return_value = _mock_session_with_results(q1)

        result = await _get_symbol_usages(42, "Unknown")
        assert result.success is True
        assert result.data.get("usages", []) == []

    @patch("src.tools.db.graph_queries.async_session")
    async def test_error(self, mock_factory):
        mock_factory.side_effect = Exception("db error")
        result = await _get_symbol_usages(42, "User")
        assert result.success is False


# ---------------------------------------------------------------------------
# _get_milestone_file_coverage
# ---------------------------------------------------------------------------

class TestGetMilestoneFileCoverage:
    @patch("src.tools.db.graph_queries.async_session")
    async def test_with_coverage(self, mock_factory):
        # Milestone files query
        q1 = MagicMock(all=MagicMock(return_value=[
            ("src/auth.py", 0.9),
        ]))
        # PR file changes query
        q2 = MagicMock(scalars=MagicMock(return_value=MagicMock(
            all=MagicMock(return_value=["src/auth.py"])
        )))
        mock_factory.return_value = _mock_session_with_results(q1, q2)

        result = await _get_milestone_file_coverage(42, 1)
        assert result.success is True
        assert result.data["total_files"] >= 1

    @patch("src.tools.db.graph_queries.async_session")
    async def test_no_files(self, mock_factory):
        q1 = MagicMock(all=MagicMock(return_value=[]))
        q2 = MagicMock(scalars=MagicMock(return_value=MagicMock(
            all=MagicMock(return_value=[])
        )))
        mock_factory.return_value = _mock_session_with_results(q1, q2)

        result = await _get_milestone_file_coverage(42, 1)
        assert result.success is True

    @patch("src.tools.db.graph_queries.async_session")
    async def test_error(self, mock_factory):
        mock_factory.side_effect = Exception("db error")
        result = await _get_milestone_file_coverage(42, 1)
        assert result.success is False


# ---------------------------------------------------------------------------
# _get_focused_code_map
# ---------------------------------------------------------------------------

class TestGetFocusedCodeMap:
    @patch("src.tools.db.graph_queries.generate_code_map")
    @patch("src.tools.db.graph_queries.async_session")
    async def test_with_records(self, mock_factory, mock_codemap):
        # File node IDs
        q1 = MagicMock(all=MagicMock(return_value=[(1, "src/main.py")]))
        # Neighbor query
        q2 = MagicMock(all=MagicMock(return_value=[]))
        # CodeIndex records
        q3 = MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
        mock_factory.return_value = _mock_session_with_results(q1, q2, q3)
        mock_codemap.return_value = "# Focused Code Map"

        result = await _get_focused_code_map(42, ["src/main.py"])
        assert result.success is True

    @patch("src.tools.db.graph_queries.async_session")
    async def test_no_files(self, mock_factory):
        q1 = MagicMock(all=MagicMock(return_value=[]))
        mock_factory.return_value = _mock_session_with_results(q1)

        result = await _get_focused_code_map(42, [])
        assert result.success is True

    @patch("src.tools.db.graph_queries.async_session")
    async def test_error(self, mock_factory):
        mock_factory.side_effect = Exception("db error")
        result = await _get_focused_code_map(42, ["src/main.py"])
        assert result.success is False


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

class TestFactories:
    def test_make_get_file_dependents(self):
        assert make_get_file_dependents(42).name == "get_file_dependents"

    def test_make_get_file_dependencies(self):
        assert make_get_file_dependencies(42).name == "get_file_dependencies"

    def test_make_get_blast_radius(self):
        assert make_get_blast_radius(42).name == "get_blast_radius"

    def test_make_get_file_ownership(self):
        assert make_get_file_ownership(42).name == "get_file_ownership"

    def test_make_get_symbol_usages(self):
        assert make_get_symbol_usages(42).name == "get_symbol_usages"

    def test_make_get_milestone_file_coverage(self):
        assert make_get_milestone_file_coverage(42).name == "get_milestone_file_coverage"

    def test_make_get_focused_code_map(self):
        assert make_get_focused_code_map(42).name == "get_focused_code_map"
