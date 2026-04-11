from pathlib import Path

import pytest

from gita.indexer.parsers import FileStructure, parse_file

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "javascript"


def _parse_fixture(name: str) -> FileStructure:
    path = FIXTURES_DIR / name
    content = path.read_text(encoding="utf-8")
    return parse_file(path, content, "javascript")


# ---------------------------------------------------------------------------
# simple_module.js — function declarations + imports
# ---------------------------------------------------------------------------
class TestSimpleModule:
    def test_counts(self):
        s = _parse_fixture("simple_module.js")
        assert len(s.functions) == 3
        assert len(s.classes) == 0
        assert len(s.imports) == 2

    def test_function_names(self):
        s = _parse_fixture("simple_module.js")
        assert {f.name for f in s.functions} == {"add", "multiply", "fetchData"}

    def test_async_detected(self):
        s = _parse_fixture("simple_module.js")
        fetch = next(f for f in s.functions if f.name == "fetchData")
        assert fetch.kind == "async_function"

    def test_sync_functions_tagged(self):
        s = _parse_fixture("simple_module.js")
        add = next(f for f in s.functions if f.name == "add")
        assert add.kind == "function"
        assert add.parent_class is None


# ---------------------------------------------------------------------------
# class_heavy.js — classes, methods, async method, inheritance via extends
# ---------------------------------------------------------------------------
class TestClassHeavy:
    def test_class_count(self):
        s = _parse_fixture("class_heavy.js")
        assert len(s.classes) == 2
        assert {c.name for c in s.classes} == {"Dog", "Cat"}
        assert all(c.kind == "class" for c in s.classes)

    def test_no_interfaces(self):
        """JavaScript has no interfaces."""
        s = _parse_fixture("class_heavy.js")
        assert not any(c.kind == "interface" for c in s.classes)

    def test_method_count(self):
        s = _parse_fixture("class_heavy.js")
        methods = [f for f in s.functions if f.kind in ("method", "async_method")]
        # Dog: constructor, sound, fetch. Cat: constructor, sound. Total 5.
        assert len(methods) == 5

    def test_parent_class_attribution(self):
        s = _parse_fixture("class_heavy.js")
        by_parent: dict[str, set[str]] = {}
        for f in s.functions:
            if f.parent_class:
                by_parent.setdefault(f.parent_class, set()).add(f.name)
        assert by_parent["Dog"] == {"constructor", "sound", "fetch"}
        assert by_parent["Cat"] == {"constructor", "sound"}

    def test_fetch_is_async_method(self):
        s = _parse_fixture("class_heavy.js")
        fetch = next(f for f in s.functions if f.name == "fetch")
        assert fetch.kind == "async_method"
        assert fetch.parent_class == "Dog"

    def test_method_lines_inside_parent_class(self):
        s = _parse_fixture("class_heavy.js")
        class_ranges = {c.name: (c.start_line, c.end_line) for c in s.classes}
        for m in s.functions:
            if m.parent_class:
                cs, ce = class_ranges[m.parent_class]
                assert cs <= m.start_line <= ce
                assert cs <= m.end_line <= ce


# ---------------------------------------------------------------------------
# arrow_heavy.js — const foo = () => { ... }
# ---------------------------------------------------------------------------
class TestArrowHeavy:
    def test_counts(self):
        s = _parse_fixture("arrow_heavy.js")
        assert len(s.functions) == 4
        assert len(s.classes) == 0
        assert len(s.imports) == 1

    def test_arrow_names(self):
        s = _parse_fixture("arrow_heavy.js")
        assert {f.name for f in s.functions} == {
            "handleClick",
            "square",
            "asyncFetch",
            "parseConfig",
        }

    def test_async_arrow(self):
        s = _parse_fixture("arrow_heavy.js")
        fetch = next(f for f in s.functions if f.name == "asyncFetch")
        assert fetch.kind == "async_function"

    def test_single_line_arrow_allowed(self):
        s = _parse_fixture("arrow_heavy.js")
        square = next(f for f in s.functions if f.name == "square")
        assert square.start_line == square.end_line


# ---------------------------------------------------------------------------
# Structural invariants across all JS fixtures
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "fixture",
    ["simple_module.js", "class_heavy.js", "arrow_heavy.js"],
)
def test_all_symbols_have_valid_lines(fixture):
    s = _parse_fixture(fixture)
    for symbol in [*s.functions, *s.classes]:
        assert symbol.start_line >= 1
        assert symbol.end_line >= symbol.start_line


@pytest.mark.parametrize(
    "fixture",
    ["simple_module.js", "class_heavy.js", "arrow_heavy.js"],
)
def test_structure_serialization(fixture):
    import json

    s = _parse_fixture(fixture)
    data = s.to_jsonb()
    assert set(data.keys()) == {"functions", "classes", "imports"}
    json.dumps(data)
