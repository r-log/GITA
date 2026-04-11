"""
Verify every parser emits `end_line` on functions and classes so the
granular code_retrieval tools can slice source by symbol.

Python uses stdlib `ast` (reference implementation). Other languages
use Tree-sitter — they're covered indirectly via real-file parsing here;
more thorough multi-language fixtures live in test_tree_sitter_parser.py.
"""

from src.indexer.parsers import parse_file


PYTHON_SAMPLE = '''"""Sample module."""

import os


def top_level_function(arg):
    """A top-level function."""
    if arg:
        return arg * 2
    return None


class Foo:
    """A class."""

    field_a: int = 0

    def method_one(self, x):
        return x + 1

    async def method_two(self):
        await self.method_one(1)
        return "done"
'''


class TestPythonEndLine:
    def test_functions_have_end_line(self):
        result = parse_file(PYTHON_SAMPLE, "sample.py")
        functions = result.structure.get("functions", [])
        assert functions, "expected at least one top-level function"
        for fn in functions:
            assert fn.get("line") is not None
            assert fn.get("end_line") is not None
            assert fn["end_line"] >= fn["line"], f"{fn['name']} end_line < line"

    def test_top_level_function_range(self):
        result = parse_file(PYTHON_SAMPLE, "sample.py")
        fn = next(
            f for f in result.structure["functions"]
            if f["name"] == "top_level_function"
        )
        # def is on a specific line in the sample above; just verify the span
        assert fn["end_line"] - fn["line"] >= 3

    def test_classes_have_end_line(self):
        result = parse_file(PYTHON_SAMPLE, "sample.py")
        classes = result.structure.get("classes", [])
        assert len(classes) == 1
        foo = classes[0]
        assert foo["name"] == "Foo"
        assert foo.get("end_line") is not None
        assert foo["end_line"] > foo["line"]

    def test_methods_have_end_line(self):
        result = parse_file(PYTHON_SAMPLE, "sample.py")
        foo = result.structure["classes"][0]
        method_details = foo.get("method_details") or []
        assert len(method_details) == 2
        for m in method_details:
            assert m.get("line") is not None
            assert m.get("end_line") is not None
            assert m["end_line"] >= m["line"]

    def test_class_span_encloses_methods(self):
        result = parse_file(PYTHON_SAMPLE, "sample.py")
        foo = result.structure["classes"][0]
        for m in foo["method_details"]:
            assert foo["line"] <= m["line"] <= foo["end_line"]
            assert foo["line"] <= m["end_line"] <= foo["end_line"]
