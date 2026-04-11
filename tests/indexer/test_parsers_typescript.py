from pathlib import Path

import pytest

from gita.indexer.parsers import FileStructure, parse_file

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "typescript"


def _parse_fixture(name: str) -> FileStructure:
    path = FIXTURES_DIR / name
    content = path.read_text(encoding="utf-8")
    return parse_file(path, content, "typescript")


# ---------------------------------------------------------------------------
# simple_module.ts — function declarations + imports
# ---------------------------------------------------------------------------
class TestSimpleModule:
    def test_counts(self):
        s = _parse_fixture("simple_module.ts")
        assert len(s.functions) == 3
        assert len(s.classes) == 0
        assert len(s.imports) == 2

    def test_function_names(self):
        s = _parse_fixture("simple_module.ts")
        assert {f.name for f in s.functions} == {"add", "multiply", "fetchData"}

    def test_async_detected(self):
        s = _parse_fixture("simple_module.ts")
        fetch = next(f for f in s.functions if f.name == "fetchData")
        assert fetch.kind == "async_function"

    def test_sync_functions_tagged(self):
        s = _parse_fixture("simple_module.ts")
        add = next(f for f in s.functions if f.name == "add")
        assert add.kind == "function"
        assert add.parent_class is None

    def test_add_line_range(self):
        """add() is on lines 6-8 of simple_module.ts."""
        s = _parse_fixture("simple_module.ts")
        add = next(f for f in s.functions if f.name == "add")
        assert add.start_line == 6
        assert add.end_line == 8


# ---------------------------------------------------------------------------
# class_heavy.ts — classes, interface, methods, async method
# ---------------------------------------------------------------------------
class TestClassHeavy:
    def test_class_and_interface_counts(self):
        s = _parse_fixture("class_heavy.ts")
        # 2 classes + 1 interface, all stored under s.classes
        assert len(s.classes) == 3
        classes = {c.name for c in s.classes if c.kind == "class"}
        interfaces = {c.name for c in s.classes if c.kind == "interface"}
        assert classes == {"Dog", "Cat"}
        assert interfaces == {"Animal"}

    def test_method_count(self):
        s = _parse_fixture("class_heavy.ts")
        methods = [f for f in s.functions if f.kind in ("method", "async_method")]
        # Dog: constructor, sound, fetch. Cat: constructor, sound. Total 5.
        assert len(methods) == 5

    def test_methods_have_parent_class(self):
        s = _parse_fixture("class_heavy.ts")
        assert all(m.parent_class is not None for m in s.functions)

    def test_parent_class_attribution(self):
        s = _parse_fixture("class_heavy.ts")
        by_parent: dict[str, set[str]] = {}
        for f in s.functions:
            if f.parent_class:
                by_parent.setdefault(f.parent_class, set()).add(f.name)
        assert by_parent["Dog"] == {"constructor", "sound", "fetch"}
        assert by_parent["Cat"] == {"constructor", "sound"}

    def test_fetch_is_async_method(self):
        s = _parse_fixture("class_heavy.ts")
        fetch = next(f for f in s.functions if f.name == "fetch")
        assert fetch.kind == "async_method"
        assert fetch.parent_class == "Dog"

    def test_method_lines_inside_parent_class(self):
        s = _parse_fixture("class_heavy.ts")
        class_ranges = {
            c.name: (c.start_line, c.end_line)
            for c in s.classes
            if c.kind == "class"
        }
        for m in s.functions:
            if m.parent_class in class_ranges:
                cs, ce = class_ranges[m.parent_class]
                assert cs <= m.start_line <= ce
                assert cs <= m.end_line <= ce


# ---------------------------------------------------------------------------
# arrow_heavy.ts — const foo = () => { ... } patterns
# ---------------------------------------------------------------------------
class TestArrowHeavy:
    def test_counts(self):
        s = _parse_fixture("arrow_heavy.ts")
        assert len(s.functions) == 4
        assert len(s.classes) == 0
        assert len(s.imports) == 1

    def test_arrow_names(self):
        s = _parse_fixture("arrow_heavy.ts")
        assert {f.name for f in s.functions} == {
            "handleClick",
            "square",
            "asyncFetch",
            "parseConfig",
        }

    def test_async_arrow(self):
        s = _parse_fixture("arrow_heavy.ts")
        fetch = next(f for f in s.functions if f.name == "asyncFetch")
        assert fetch.kind == "async_function"

    def test_sync_arrows(self):
        s = _parse_fixture("arrow_heavy.ts")
        for name in ("handleClick", "square", "parseConfig"):
            f = next(x for x in s.functions if x.name == name)
            assert f.kind == "function"

    def test_single_line_arrow_allowed(self):
        """square = (x) => x * x; is a single-line arrow (start == end)."""
        s = _parse_fixture("arrow_heavy.ts")
        square = next(f for f in s.functions if f.name == "square")
        assert square.start_line == square.end_line


# ---------------------------------------------------------------------------
# Structural invariants across all TS fixtures
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "fixture",
    ["simple_module.ts", "class_heavy.ts", "arrow_heavy.ts"],
)
def test_all_symbols_have_valid_lines(fixture):
    s = _parse_fixture(fixture)
    for symbol in [*s.functions, *s.classes]:
        assert symbol.start_line >= 1
        assert symbol.end_line >= symbol.start_line


@pytest.mark.parametrize(
    "fixture",
    ["simple_module.ts", "class_heavy.ts", "arrow_heavy.ts"],
)
def test_structure_serialization(fixture):
    import json

    s = _parse_fixture(fixture)
    data = s.to_jsonb()
    assert set(data.keys()) == {"functions", "classes", "imports"}
    json.dumps(data)
