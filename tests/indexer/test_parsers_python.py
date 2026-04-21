from pathlib import Path

import pytest

from gita.indexer.parsers import FileStructure, parse_file

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "python"


def _parse_fixture(name: str) -> FileStructure:
    path = FIXTURES_DIR / name
    content = path.read_text(encoding="utf-8")
    return parse_file(path, content, "python")


# ---------------------------------------------------------------------------
# simple_module.py — top-level functions only
# ---------------------------------------------------------------------------
class TestSimpleModule:
    def test_counts(self):
        s = _parse_fixture("simple_module.py")
        assert len(s.functions) == 3
        assert len(s.classes) == 0
        assert len(s.imports) == 2

    def test_function_names(self):
        s = _parse_fixture("simple_module.py")
        names = {f.name for f in s.functions}
        assert names == {"add", "multiply", "fetch_data"}

    def test_async_detected(self):
        s = _parse_fixture("simple_module.py")
        fetch = next(f for f in s.functions if f.name == "fetch_data")
        assert fetch.kind == "async_function"

    def test_sync_functions_tagged(self):
        s = _parse_fixture("simple_module.py")
        add = next(f for f in s.functions if f.name == "add")
        assert add.kind == "function"
        assert add.parent_class is None

    def test_line_ranges_valid(self):
        s = _parse_fixture("simple_module.py")
        for func in s.functions:
            assert func.start_line >= 1, f"{func.name} has non-positive start_line"
            assert func.end_line >= func.start_line, (
                f"{func.name} end_line {func.end_line} < start_line {func.start_line}"
            )

    def test_add_line_numbers(self):
        """add() is on lines 8-10 of simple_module.py."""
        s = _parse_fixture("simple_module.py")
        add = next(f for f in s.functions if f.name == "add")
        assert add.start_line == 8
        assert add.end_line == 10


# ---------------------------------------------------------------------------
# class_heavy.py — classes, methods, async method, inheritance
# ---------------------------------------------------------------------------
class TestClassHeavy:
    def test_class_count(self):
        s = _parse_fixture("class_heavy.py")
        assert len(s.classes) == 3
        names = {c.name for c in s.classes}
        assert names == {"Animal", "Dog", "Cat"}

    def test_all_class_kinds(self):
        s = _parse_fixture("class_heavy.py")
        assert all(c.kind == "class" for c in s.classes)

    def test_method_count(self):
        s = _parse_fixture("class_heavy.py")
        methods = [f for f in s.functions if f.kind in ("method", "async_method")]
        # __init__, Animal.sound, Dog.sound, Dog.fetch, Cat.sound, Cat.nap = 6
        assert len(methods) == 6

    def test_methods_have_parent_class(self):
        s = _parse_fixture("class_heavy.py")
        methods = [f for f in s.functions if f.kind in ("method", "async_method")]
        assert all(m.parent_class is not None for m in methods)

    def test_parent_class_attribution(self):
        s = _parse_fixture("class_heavy.py")
        by_parent: dict[str, set[str]] = {}
        for f in s.functions:
            if f.parent_class:
                by_parent.setdefault(f.parent_class, set()).add(f.name)
        assert by_parent["Animal"] == {"__init__", "sound"}
        assert by_parent["Dog"] == {"sound", "fetch"}
        assert by_parent["Cat"] == {"sound", "nap"}

    def test_nap_is_async_method(self):
        s = _parse_fixture("class_heavy.py")
        nap = next(f for f in s.functions if f.name == "nap")
        assert nap.kind == "async_method"
        assert nap.parent_class == "Cat"

    def test_no_top_level_functions(self):
        s = _parse_fixture("class_heavy.py")
        top_level = [f for f in s.functions if f.parent_class is None]
        assert top_level == []

    def test_class_line_ranges_contain_methods(self):
        """Every method's line range must be inside its parent class's range."""
        s = _parse_fixture("class_heavy.py")
        class_ranges = {c.name: (c.start_line, c.end_line) for c in s.classes}
        for m in s.functions:
            if m.parent_class:
                cs, ce = class_ranges[m.parent_class]
                assert cs <= m.start_line <= ce, (
                    f"{m.parent_class}.{m.name} start {m.start_line} outside class {cs}-{ce}"
                )
                assert cs <= m.end_line <= ce


# ---------------------------------------------------------------------------
# decorator_heavy.py — decorators, nested functions, @staticmethod/@classmethod
# ---------------------------------------------------------------------------
class TestDecoratorHeavy:
    def test_class_found(self):
        s = _parse_fixture("decorator_heavy.py")
        assert len(s.classes) == 1
        assert s.classes[0].name == "Service"

    def test_top_level_and_nested_captured(self):
        s = _parse_fixture("decorator_heavy.py")
        top_level = {f.name for f in s.functions if f.parent_class is None}
        # wrapper is nested inside my_decorator but still captured as a function
        assert top_level == {"my_decorator", "wrapper", "decorated_function"}

    def test_service_methods(self):
        s = _parse_fixture("decorator_heavy.py")
        service_methods = {
            f.name for f in s.functions if f.parent_class == "Service"
        }
        assert service_methods == {"static_method", "class_method", "name"}

    def test_service_methods_are_methods(self):
        s = _parse_fixture("decorator_heavy.py")
        for f in s.functions:
            if f.parent_class == "Service":
                assert f.kind == "method"


# ---------------------------------------------------------------------------
# Structural invariants across all fixtures
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "fixture",
    ["simple_module.py", "class_heavy.py", "decorator_heavy.py"],
)
def test_all_symbols_have_valid_lines(fixture):
    s = _parse_fixture(fixture)
    for symbol in [*s.functions, *s.classes]:
        assert symbol.start_line >= 1
        assert symbol.end_line >= symbol.start_line


@pytest.mark.parametrize(
    "fixture",
    ["simple_module.py", "class_heavy.py", "decorator_heavy.py"],
)
def test_structure_serialization(fixture):
    """FileStructure.to_jsonb() must produce a serializable dict."""
    import json

    s = _parse_fixture(fixture)
    data = s.to_jsonb()
    assert set(data.keys()) == {"functions", "classes", "imports"}
    json.dumps(data)  # raises if anything is not serializable


# ---------------------------------------------------------------------------
# Signature extraction
# ---------------------------------------------------------------------------
class TestSignatureExtraction:
    def test_typed_params_and_return(self):
        s = _parse_fixture("simple_module.py")
        add = next(f for f in s.functions if f.name == "add")
        assert add.signature == "def add(a: int, b: int) -> int"

    def test_no_return_type(self):
        s = _parse_fixture("simple_module.py")
        mul = next(f for f in s.functions if f.name == "multiply")
        assert mul.signature == "def multiply(a: int, b: int) -> int"

    def test_async_function_signature(self):
        s = _parse_fixture("simple_module.py")
        fetch = next(f for f in s.functions if f.name == "fetch_data")
        assert fetch.signature is not None
        assert fetch.signature.startswith("async def fetch_data(")
        assert "-> Optional[str]" in fetch.signature

    def test_method_signature(self):
        s = _parse_fixture("decorator_heavy.py")
        name_fn = next(f for f in s.functions if f.name == "name")
        assert name_fn.signature is not None
        assert "self" in name_fn.signature
        assert "-> str" in name_fn.signature

    def test_no_params_function(self):
        s = _parse_fixture("decorator_heavy.py")
        dec = next(f for f in s.functions if f.name == "decorated_function")
        assert dec.signature == "def decorated_function()"


# ---------------------------------------------------------------------------
# Docstring extraction
# ---------------------------------------------------------------------------
class TestDocstringExtraction:
    def test_single_line_docstring(self):
        s = _parse_fixture("simple_module.py")
        add = next(f for f in s.functions if f.name == "add")
        assert add.docstring == "Add two numbers."

    def test_no_docstring(self):
        s = _parse_fixture("simple_module.py")
        mul = next(f for f in s.functions if f.name == "multiply")
        assert mul.docstring is None

    def test_function_without_body_docstring(self):
        s = _parse_fixture("decorator_heavy.py")
        dec = next(f for f in s.functions if f.name == "decorated_function")
        assert dec.docstring is None

    def test_module_docstring_not_on_class(self):
        """Module-level docstrings shouldn't leak onto functions."""
        s = _parse_fixture("simple_module.py")
        # The module has a docstring but individual functions shouldn't pick it up
        mul = next(f for f in s.functions if f.name == "multiply")
        assert mul.docstring is None


# ---------------------------------------------------------------------------
# Decorator extraction
# ---------------------------------------------------------------------------
class TestDecoratorExtraction:
    def test_single_decorator(self):
        s = _parse_fixture("decorator_heavy.py")
        dec = next(f for f in s.functions if f.name == "decorated_function")
        assert dec.decorators == ["@my_decorator"]

    def test_staticmethod(self):
        s = _parse_fixture("decorator_heavy.py")
        sm = next(f for f in s.functions if f.name == "static_method")
        assert "@staticmethod" in sm.decorators

    def test_classmethod(self):
        s = _parse_fixture("decorator_heavy.py")
        cm = next(f for f in s.functions if f.name == "class_method")
        assert "@classmethod" in cm.decorators

    def test_property_decorator(self):
        s = _parse_fixture("decorator_heavy.py")
        name_fn = next(f for f in s.functions if f.name == "name")
        assert "@property" in name_fn.decorators

    def test_no_decorators(self):
        s = _parse_fixture("simple_module.py")
        add = next(f for f in s.functions if f.name == "add")
        assert add.decorators == []

    def test_serialization_includes_new_fields(self):
        """JSONB output includes signature, docstring, decorators."""
        s = _parse_fixture("simple_module.py")
        data = s.to_jsonb()
        fn = next(f for f in data["functions"] if f["name"] == "add")
        assert "signature" in fn
        assert "docstring" in fn
        assert "decorators" in fn
