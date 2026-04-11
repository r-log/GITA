"""Import resolution tests for Python and TS/JS.

Each test builds a tiny file tree in tmp_path so the resolver has real files
to match against.
"""
from pathlib import Path

import pytest

from gita.indexer.imports import (
    resolve_import,
    resolve_python_import,
    resolve_ts_js_import,
)


def _touch(root: Path, rel: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------
class TestPythonImports:
    def test_relative_single_dot(self, tmp_path):
        pkg = tmp_path / "myapp"
        _touch(tmp_path, "myapp/utils.py")
        src = _touch(tmp_path, "myapp/models.py")

        resolved = resolve_python_import(
            "from .utils import foo", src, tmp_path
        )
        assert resolved == pkg / "utils.py"

    def test_relative_double_dot(self, tmp_path):
        _touch(tmp_path, "myapp/utils.py")
        src = _touch(tmp_path, "myapp/sub/core.py")

        resolved = resolve_python_import(
            "from ..utils import foo", src, tmp_path
        )
        assert resolved == (tmp_path / "myapp" / "utils.py")

    def test_relative_bare_from_dot_import_name(self, tmp_path):
        """`from . import x` → this package's __init__.py"""
        _touch(tmp_path, "myapp/__init__.py")
        src = _touch(tmp_path, "myapp/core.py")

        resolved = resolve_python_import("from . import x", src, tmp_path)
        assert resolved == tmp_path / "myapp" / "__init__.py"

    def test_absolute_from_import(self, tmp_path):
        _touch(tmp_path, "pkg/sub.py")
        src = _touch(tmp_path, "elsewhere.py")

        resolved = resolve_python_import(
            "from pkg.sub import foo", src, tmp_path
        )
        assert resolved == tmp_path / "pkg" / "sub.py"

    def test_absolute_as_package_init(self, tmp_path):
        _touch(tmp_path, "pkg/sub/__init__.py")
        src = _touch(tmp_path, "elsewhere.py")

        resolved = resolve_python_import(
            "from pkg.sub import foo", src, tmp_path
        )
        assert resolved == tmp_path / "pkg" / "sub" / "__init__.py"

    def test_plain_import_resolves_to_repo_file(self, tmp_path):
        _touch(tmp_path, "myutil.py")
        src = _touch(tmp_path, "caller.py")

        resolved = resolve_python_import("import myutil", src, tmp_path)
        assert resolved == tmp_path / "myutil.py"

    def test_stdlib_import_unresolved(self, tmp_path):
        src = _touch(tmp_path, "caller.py")
        assert resolve_python_import("import os", src, tmp_path) is None
        assert (
            resolve_python_import("from typing import List", src, tmp_path)
            is None
        )

    def test_malformed_returns_none(self, tmp_path):
        src = _touch(tmp_path, "caller.py")
        assert resolve_python_import("not an import", src, tmp_path) is None


# ---------------------------------------------------------------------------
# TypeScript / JavaScript
# ---------------------------------------------------------------------------
class TestTsJsImports:
    def test_relative_ts_extension_inferred(self, tmp_path):
        _touch(tmp_path, "src/utils.ts")
        src = _touch(tmp_path, "src/index.ts")

        resolved = resolve_ts_js_import(
            "import { foo } from './utils';", src, tmp_path
        )
        assert resolved == tmp_path / "src" / "utils.ts"

    def test_relative_js_extension_inferred(self, tmp_path):
        _touch(tmp_path, "src/utils.js")
        src = _touch(tmp_path, "src/index.js")

        resolved = resolve_ts_js_import(
            "import foo from './utils';", src, tmp_path
        )
        assert resolved == tmp_path / "src" / "utils.js"

    def test_ts_preferred_over_js_when_both_exist(self, tmp_path):
        _touch(tmp_path, "src/utils.ts")
        _touch(tmp_path, "src/utils.js")
        src = _touch(tmp_path, "src/index.ts")

        resolved = resolve_ts_js_import(
            "import x from './utils';", src, tmp_path
        )
        assert resolved == tmp_path / "src" / "utils.ts"

    def test_parent_directory(self, tmp_path):
        _touch(tmp_path, "shared/util.ts")
        src = _touch(tmp_path, "app/feature/index.ts")

        resolved = resolve_ts_js_import(
            "import { x } from '../../shared/util';", src, tmp_path
        )
        assert resolved == tmp_path / "shared" / "util.ts"

    def test_directory_with_index(self, tmp_path):
        _touch(tmp_path, "components/Button/index.tsx")
        src = _touch(tmp_path, "app.tsx")

        resolved = resolve_ts_js_import(
            "import Button from './components/Button';", src, tmp_path
        )
        assert resolved == tmp_path / "components" / "Button" / "index.tsx"

    def test_bare_package_unresolved(self, tmp_path):
        src = _touch(tmp_path, "src/index.ts")

        assert (
            resolve_ts_js_import(
                "import React from 'react';", src, tmp_path
            )
            is None
        )
        assert (
            resolve_ts_js_import(
                "import { foo } from '@scope/pkg';", src, tmp_path
            )
            is None
        )

    def test_explicit_extension_preserved(self, tmp_path):
        _touch(tmp_path, "src/data.js")
        src = _touch(tmp_path, "src/main.js")

        resolved = resolve_ts_js_import(
            "import d from './data.js';", src, tmp_path
        )
        assert resolved == tmp_path / "src" / "data.js"

    def test_escape_from_repo_returns_none(self, tmp_path):
        src = _touch(tmp_path, "src/main.ts")

        # Crafted path goes above tmp_path
        result = resolve_ts_js_import(
            "import x from '../../../../../../etc/passwd';", src, tmp_path
        )
        assert result is None

    def test_double_quotes(self, tmp_path):
        _touch(tmp_path, "src/utils.ts")
        src = _touch(tmp_path, "src/main.ts")

        resolved = resolve_ts_js_import(
            'import x from "./utils";', src, tmp_path
        )
        assert resolved == tmp_path / "src" / "utils.ts"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
class TestResolveImportDispatcher:
    def test_python_language(self, tmp_path):
        _touch(tmp_path, "foo.py")
        src = _touch(tmp_path, "bar.py")
        assert resolve_import("import foo", src, tmp_path, "python") == (
            tmp_path / "foo.py"
        )

    def test_typescript_language(self, tmp_path):
        _touch(tmp_path, "foo.ts")
        src = _touch(tmp_path, "bar.ts")
        assert resolve_import(
            "import x from './foo';", src, tmp_path, "typescript"
        ) == tmp_path / "foo.ts"

    def test_unknown_language_returns_none(self, tmp_path):
        src = _touch(tmp_path, "a.py")
        assert resolve_import("import foo", src, tmp_path, "ruby") is None
