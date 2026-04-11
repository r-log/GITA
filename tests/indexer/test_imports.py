"""Import resolution tests for Python and TS/JS.

Each test builds a tiny file tree in tmp_path so the resolver has real files
to match against.
"""
from pathlib import Path

from gita.indexer.imports import (
    discover_package_roots,
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


# ---------------------------------------------------------------------------
# Package-root discovery (Week 2 P1 fix)
# ---------------------------------------------------------------------------
class TestDiscoverPackageRoots:
    def test_flat_repo_returns_only_repo_root(self, tmp_path):
        """A repo with no __init__.py anywhere → just the repo root."""
        _touch(tmp_path, "script.py")
        _touch(tmp_path, "other.py")
        roots = discover_package_roots(tmp_path)
        assert roots == [tmp_path.resolve()]

    def test_flat_package_at_repo_root(self, tmp_path):
        """pkg/__init__.py at repo root → repo root is a package root."""
        _touch(tmp_path, "mypkg/__init__.py")
        _touch(tmp_path, "mypkg/core.py")
        roots = discover_package_roots(tmp_path)
        # Only the repo root — mypkg/'s parent IS repo_root, which is dedup'd
        assert roots == [tmp_path.resolve()]

    def test_src_layout(self, tmp_path):
        """src/mypkg/__init__.py → ['src/', repo_root]"""
        _touch(tmp_path, "src/mypkg/__init__.py")
        _touch(tmp_path, "src/mypkg/core.py")
        roots = discover_package_roots(tmp_path)
        assert (tmp_path / "src").resolve() in roots
        assert tmp_path.resolve() in roots
        # Repo root comes last as fallback
        assert roots[-1] == tmp_path.resolve()

    def test_backend_layout(self, tmp_path):
        """backend/app/__init__.py → ['backend/', repo_root]"""
        _touch(tmp_path, "backend/app/__init__.py")
        _touch(tmp_path, "backend/app/models.py")
        _touch(tmp_path, "backend/app/api/__init__.py")
        _touch(tmp_path, "backend/app/api/routes.py")
        roots = discover_package_roots(tmp_path)
        assert (tmp_path / "backend").resolve() in roots
        assert roots[-1] == tmp_path.resolve()

    def test_nested_packages_collapse_to_one_root(self, tmp_path):
        """A deep package chain (a/b/c/d with __init__.py at every level)
        should still only contribute ONE package root: parent of the topmost
        package."""
        _touch(tmp_path, "backend/app/__init__.py")
        _touch(tmp_path, "backend/app/api/__init__.py")
        _touch(tmp_path, "backend/app/api/auth/__init__.py")
        _touch(tmp_path, "backend/app/api/auth/routes.py")
        roots = discover_package_roots(tmp_path)
        # Only `backend/` should be in the list (plus repo_root as fallback)
        assert (tmp_path / "backend").resolve() in roots
        assert (tmp_path / "backend" / "app").resolve() not in roots
        assert (tmp_path / "backend" / "app" / "api").resolve() not in roots

    def test_multiple_disjoint_roots(self, tmp_path):
        """Two completely separate package trees → both are roots."""
        _touch(tmp_path, "backend/app/__init__.py")
        _touch(tmp_path, "tools/lib/__init__.py")
        roots = discover_package_roots(tmp_path)
        assert (tmp_path / "backend").resolve() in roots
        assert (tmp_path / "tools").resolve() in roots

    def test_node_modules_excluded(self, tmp_path):
        """__init__.py files inside skipped dirs must not produce package roots."""
        _touch(tmp_path, "node_modules/bad/__init__.py")
        _touch(tmp_path, "real/pkg/__init__.py")
        roots = discover_package_roots(tmp_path)
        assert (tmp_path / "real").resolve() in roots
        assert (tmp_path / "node_modules").resolve() not in roots

    def test_venv_excluded(self, tmp_path):
        _touch(tmp_path, ".venv/lib/site-packages/foo/__init__.py")
        _touch(tmp_path, "src/mypkg/__init__.py")
        roots = discover_package_roots(tmp_path)
        assert (tmp_path / ".venv").resolve() not in roots
        assert (tmp_path / "src").resolve() in roots

    def test_test_dirs_excluded(self, tmp_path):
        """A tests/ package shouldn't pollute the package-root list."""
        _touch(tmp_path, "src/mypkg/__init__.py")
        _touch(tmp_path, "tests/__init__.py")
        _touch(tmp_path, "tests/test_foo.py")
        roots = discover_package_roots(tmp_path)
        assert (tmp_path / "src").resolve() in roots
        # The repo-root fallback is always present, but tests/ itself shouldn't
        # appear as a distinct root.
        assert (tmp_path / "tests").resolve() not in roots

    def test_exclusion_only_applies_inside_repo_root(self, tmp_path):
        """If the ABSOLUTE path leading to the repo contains an excluded
        directory name (e.g. the repo lives under /foo/tests/bar/), that
        must not cause every __init__.py inside the repo to be skipped.
        Only path components INSIDE the repo root should be checked."""
        repo = tmp_path / "tests" / "fixtures" / "myrepo"
        repo.mkdir(parents=True)
        _touch(repo, "src/mypkg/__init__.py")
        _touch(repo, "src/mypkg/core.py")
        roots = discover_package_roots(repo)
        # src/ must still be discovered despite "tests" appearing in the
        # absolute path above the repo root.
        assert (repo / "src").resolve() in roots


# ---------------------------------------------------------------------------
# Absolute imports via package_roots (Week 2 P1 fix)
# ---------------------------------------------------------------------------
class TestPythonImportsWithPackageRoots:
    def test_absolute_import_resolves_via_src_layout(self, tmp_path):
        """`from mypkg.utils import foo` in a src-layout repo should resolve
        to src/mypkg/utils.py when src/ is in package_roots."""
        _touch(tmp_path, "src/mypkg/__init__.py")
        _touch(tmp_path, "src/mypkg/utils.py")
        src = _touch(tmp_path, "src/mypkg/models.py")

        package_roots = discover_package_roots(tmp_path)
        resolved = resolve_python_import(
            "from mypkg.utils import format_name",
            src,
            tmp_path,
            package_roots,
        )
        assert resolved == (tmp_path / "src" / "mypkg" / "utils.py").resolve()

    def test_absolute_import_resolves_via_backend_layout(self, tmp_path):
        """`from app.models import User` → backend/app/models.py"""
        _touch(tmp_path, "backend/app/__init__.py")
        _touch(tmp_path, "backend/app/models.py")
        _touch(tmp_path, "backend/app/api/__init__.py")
        src = _touch(tmp_path, "backend/app/api/routes.py")

        package_roots = discover_package_roots(tmp_path)
        resolved = resolve_python_import(
            "from app.models import User", src, tmp_path, package_roots
        )
        assert resolved == (
            tmp_path / "backend" / "app" / "models.py"
        ).resolve()

    def test_plain_import_via_src_layout(self, tmp_path):
        _touch(tmp_path, "src/mypkg/__init__.py")
        _touch(tmp_path, "src/mypkg/helpers.py")
        src = _touch(tmp_path, "src/mypkg/service.py")

        package_roots = discover_package_roots(tmp_path)
        resolved = resolve_python_import(
            "import mypkg.helpers", src, tmp_path, package_roots
        )
        assert resolved == (
            tmp_path / "src" / "mypkg" / "helpers.py"
        ).resolve()

    def test_relative_imports_still_work(self, tmp_path):
        """Relative imports should resolve against the source file's dir,
        not go through package_roots. Regression check."""
        _touch(tmp_path, "backend/app/__init__.py")
        _touch(tmp_path, "backend/app/utils.py")
        src = _touch(tmp_path, "backend/app/models.py")

        package_roots = discover_package_roots(tmp_path)
        resolved = resolve_python_import(
            "from .utils import foo", src, tmp_path, package_roots
        )
        assert resolved == (
            tmp_path / "backend" / "app" / "utils.py"
        ).resolve()

    def test_stdlib_still_unresolved(self, tmp_path):
        """`import os` shouldn't suddenly resolve just because package_roots
        is richer — stdlib doesn't live in the repo."""
        _touch(tmp_path, "backend/app/__init__.py")
        src = _touch(tmp_path, "backend/app/models.py")

        package_roots = discover_package_roots(tmp_path)
        assert (
            resolve_python_import("import os", src, tmp_path, package_roots)
            is None
        )
        assert (
            resolve_python_import(
                "from typing import List", src, tmp_path, package_roots
            )
            is None
        )

    def test_fallback_to_repo_root_when_no_packages(self, tmp_path):
        """Repo with no __init__.py at all → absolute imports resolved
        against repo root as fallback."""
        _touch(tmp_path, "myutil.py")
        src = _touch(tmp_path, "caller.py")

        package_roots = discover_package_roots(tmp_path)
        resolved = resolve_python_import(
            "import myutil", src, tmp_path, package_roots
        )
        assert resolved == (tmp_path / "myutil.py").resolve()
