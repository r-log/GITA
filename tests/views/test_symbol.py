import pytest

from gita.views._common import RepoNotFoundError
from gita.views.symbol import SymbolResult, symbol_view


class TestSymbolViewBasics:
    async def test_find_top_level_function(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await symbol_view(session, repo, "format_name")
        assert isinstance(result, SymbolResult)
        assert result.total_matches == 1
        match = result.matches[0]
        assert match.name == "format_name"
        assert match.file_path == "src/myapp/utils.py"
        assert match.kind == "function"
        assert match.parent_class is None

    async def test_code_contains_line_numbers(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await symbol_view(session, repo, "format_name")
        match = result.matches[0]
        # Code should be non-empty and include the function signature
        assert "def format_name" in match.code
        # Each line should be prefixed with "N: "
        for line in match.code.splitlines():
            prefix = line.split(":", 1)[0].strip()
            assert prefix.isdigit(), f"line missing number prefix: {line!r}"

    async def test_find_class(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await symbol_view(session, repo, "User")
        assert result.total_matches == 1
        match = result.matches[0]
        assert match.name == "User"
        assert match.kind == "class"
        assert match.file_path == "src/myapp/models.py"
        assert "class User" in match.code

    async def test_class_code_includes_all_methods(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await symbol_view(session, repo, "User")
        code = result.matches[0].code
        assert "def display_name" in code
        assert "def has_valid_email" in code


class TestSymbolViewDisambiguation:
    async def test_classname_method_query(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await symbol_view(session, repo, "User.display_name")
        assert result.total_matches == 1
        match = result.matches[0]
        assert match.name == "display_name"
        assert match.parent_class == "User"
        assert match.kind == "method"

    async def test_bare_method_name_still_matches(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await symbol_view(session, repo, "display_name")
        assert result.total_matches == 1
        assert result.matches[0].parent_class == "User"


class TestSymbolViewMisses:
    async def test_unknown_symbol_returns_empty(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await symbol_view(session, repo, "nonexistent")
        assert result.total_matches == 0
        assert result.matches == []

    async def test_empty_query_returns_empty(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await symbol_view(session, repo, "")
        assert result.total_matches == 0

    async def test_classname_method_with_wrong_class(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await symbol_view(session, repo, "Dog.display_name")
        # No class Dog in synthetic_py → no match
        assert result.total_matches == 0


class TestSymbolViewErrors:
    async def test_unknown_repo_raises(self, db_session):
        with pytest.raises(RepoNotFoundError):
            await symbol_view(db_session, "does-not-exist", "foo")


class TestSymbolViewTruncation:
    async def test_truncated_flag_is_false_on_small_result(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await symbol_view(session, repo, "format_name")
        assert result.truncated is False
