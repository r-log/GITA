import pytest

from gita.views._common import RepoNotFoundError
from gita.views.neighborhood import (
    FileNotFoundError,
    NeighborhoodResult,
    neighborhood_view,
)


# ---------------------------------------------------------------------------
# core.py is the most interesting file in synthetic_py:
#   - imports .models, .utils, and os
#   - imported by nothing (it's a leaf)
#   - siblings: __init__.py, models.py, utils.py
# ---------------------------------------------------------------------------
class TestNeighborhoodCore:
    async def test_returns_file_info(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await neighborhood_view(session, repo, "src/myapp/core.py")
        assert isinstance(result, NeighborhoodResult)
        assert result.file.file_path == "src/myapp/core.py"
        assert result.file.language == "python"
        assert result.file.line_count > 0

    async def test_file_symbol_summary(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await neighborhood_view(session, repo, "src/myapp/core.py")
        names = {s.name for s in result.file.symbol_summary}
        assert "create_user" in names
        assert "main" in names

    async def test_symbol_summary_is_metadata_only(self, indexed_synth_py):
        """Make sure the symbol briefs don't contain code bodies."""
        session, repo = indexed_synth_py
        result = await neighborhood_view(session, repo, "src/myapp/core.py")
        for brief in result.file.symbol_summary:
            # SymbolBrief has no 'code' or 'content' field
            assert not hasattr(brief, "code")
            assert not hasattr(brief, "content")

    async def test_imports_resolved(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await neighborhood_view(session, repo, "src/myapp/core.py")
        import_paths = {f.file_path for f in result.imports}
        assert import_paths == {"src/myapp/models.py", "src/myapp/utils.py"}

    async def test_unresolved_imports_surfaced(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await neighborhood_view(session, repo, "src/myapp/core.py")
        # core.py imports `os` (stdlib) which should not resolve
        assert any("os" in raw for raw in result.unresolved_imports)

    async def test_core_has_no_importers(self, indexed_synth_py):
        """core.py is a leaf — nothing else imports it."""
        session, repo = indexed_synth_py
        result = await neighborhood_view(session, repo, "src/myapp/core.py")
        assert result.imported_by == []

    async def test_siblings_include_package_members(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await neighborhood_view(session, repo, "src/myapp/core.py")
        sibling_paths = {f.file_path for f in result.siblings}
        expected = {
            "src/myapp/__init__.py",
            "src/myapp/models.py",
            "src/myapp/utils.py",
        }
        assert sibling_paths == expected

    async def test_siblings_do_not_include_self(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await neighborhood_view(session, repo, "src/myapp/core.py")
        assert not any(f.file_path == "src/myapp/core.py" for f in result.siblings)


# ---------------------------------------------------------------------------
# utils.py has no imports and is imported by both models.py and core.py.
# ---------------------------------------------------------------------------
class TestNeighborhoodUtils:
    async def test_utils_has_no_imports(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await neighborhood_view(session, repo, "src/myapp/utils.py")
        assert result.imports == []
        assert result.unresolved_imports == []

    async def test_utils_is_imported_by_both(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await neighborhood_view(session, repo, "src/myapp/utils.py")
        importer_paths = {f.file_path for f in result.imported_by}
        assert importer_paths == {"src/myapp/models.py", "src/myapp/core.py"}


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------
class TestNeighborhoodErrors:
    async def test_unknown_repo(self, db_session):
        with pytest.raises(RepoNotFoundError):
            await neighborhood_view(db_session, "nope", "foo.py")

    async def test_unknown_file(self, indexed_synth_py):
        session, repo = indexed_synth_py
        with pytest.raises(FileNotFoundError):
            await neighborhood_view(session, repo, "does/not/exist.py")

    async def test_backslash_path_normalized(self, indexed_synth_py):
        session, repo = indexed_synth_py
        # Should work when caller passes Windows-style path
        result = await neighborhood_view(
            session, repo, "src\\myapp\\core.py"
        )
        assert result.file.file_path == "src/myapp/core.py"
