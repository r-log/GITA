"""Walker tests — uses tmp_path to build ad-hoc file trees and asserts the
filter yields exactly what we expect.
"""
from pathlib import Path

from gita.indexer.walker import iter_files


def _touch(root: Path, rel: str, content: str = "pass\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use binary write + explicit encoding so Windows doesn't convert \n → \r\n.
    path.write_bytes(content.encode("utf-8"))
    return path


def _collect(root: Path, **kwargs) -> set[str]:
    return {f.relative_path for f in iter_files(root, **kwargs)}


class TestLanguageMapping:
    def test_py_maps_to_python(self, tmp_path):
        _touch(tmp_path, "a.py")
        discovered = list(iter_files(tmp_path))
        assert len(discovered) == 1
        assert discovered[0].language == "python"

    def test_ts_maps_to_typescript(self, tmp_path):
        _touch(tmp_path, "a.ts")
        (lang,) = {f.language for f in iter_files(tmp_path)}
        assert lang == "typescript"

    def test_all_js_flavors_map_to_javascript(self, tmp_path):
        _touch(tmp_path, "a.js")
        _touch(tmp_path, "b.mjs")
        _touch(tmp_path, "c.cjs")
        _touch(tmp_path, "d.jsx")
        langs = {f.language for f in iter_files(tmp_path)}
        assert langs == {"javascript"}

    def test_unsupported_extension_ignored(self, tmp_path):
        _touch(tmp_path, "data.json", content="{}")
        _touch(tmp_path, "doc.md", content="# hi")
        _touch(tmp_path, "run.sh", content="#!/bin/sh\n")
        assert _collect(tmp_path) == set()


class TestSkipDirs:
    def test_node_modules_skipped(self, tmp_path):
        _touch(tmp_path, "node_modules/pkg/index.js")
        _touch(tmp_path, "src/app.js")
        assert _collect(tmp_path) == {"src/app.js"}

    def test_venv_skipped(self, tmp_path):
        _touch(tmp_path, ".venv/lib/site.py")
        _touch(tmp_path, "venv/lib/site.py")
        _touch(tmp_path, "src/app.py")
        assert _collect(tmp_path) == {"src/app.py"}

    def test_build_dirs_skipped(self, tmp_path):
        _touch(tmp_path, "dist/bundle.js")
        _touch(tmp_path, "build/output.js")
        _touch(tmp_path, "src/app.js")
        assert _collect(tmp_path) == {"src/app.js"}

    def test_cache_dirs_skipped(self, tmp_path):
        _touch(tmp_path, "__pycache__/a.py")
        _touch(tmp_path, ".pytest_cache/v/cache.py")
        _touch(tmp_path, ".mypy_cache/data.py")
        _touch(tmp_path, "src/app.py")
        assert _collect(tmp_path) == {"src/app.py"}

    def test_git_skipped(self, tmp_path):
        _touch(tmp_path, ".git/hooks/pre.py")
        _touch(tmp_path, "app.py")
        assert _collect(tmp_path) == {"app.py"}


class TestSkipSuffixes:
    def test_min_js_skipped(self, tmp_path):
        _touch(tmp_path, "dist/app.min.js")  # dist skipped too; use plain subdir
        _touch(tmp_path, "public/vendor.min.js")
        _touch(tmp_path, "public/app.js")
        assert _collect(tmp_path) == {"public/app.js"}

    def test_d_ts_skipped(self, tmp_path):
        _touch(tmp_path, "types.d.ts")
        _touch(tmp_path, "real.ts")
        assert _collect(tmp_path) == {"real.ts"}

    def test_pyi_skipped(self, tmp_path):
        _touch(tmp_path, "stubs.pyi")
        _touch(tmp_path, "real.py")
        assert _collect(tmp_path) == {"real.py"}

    def test_pb2_skipped(self, tmp_path):
        _touch(tmp_path, "service_pb2.py")
        _touch(tmp_path, "service_pb2_grpc.py")
        _touch(tmp_path, "service.py")
        assert _collect(tmp_path) == {"service.py"}


class TestTestDirs:
    def test_tests_dir_skipped_by_default(self, tmp_path):
        _touch(tmp_path, "tests/test_x.py")
        _touch(tmp_path, "src/app.py")
        assert _collect(tmp_path) == {"src/app.py"}

    def test_spec_dir_skipped(self, tmp_path):
        _touch(tmp_path, "spec/foo.spec.ts")
        _touch(tmp_path, "src/app.ts")
        assert _collect(tmp_path) == {"src/app.ts"}

    def test_include_tests_flag(self, tmp_path):
        _touch(tmp_path, "tests/test_x.py")
        _touch(tmp_path, "src/app.py")
        assert _collect(tmp_path, include_tests=True) == {
            "tests/test_x.py",
            "src/app.py",
        }


class TestSizeFilter:
    def test_oversized_file_skipped(self, tmp_path):
        big = "x" * 300
        _touch(tmp_path, "huge.py", content=big)
        _touch(tmp_path, "small.py", content="pass\n")
        # Force a tiny cap so the test is deterministic regardless of defaults
        discovered = {
            f.relative_path for f in iter_files(tmp_path, max_file_size=100)
        }
        assert discovered == {"small.py"}


class TestDiscoveredFields:
    def test_relative_path_uses_forward_slashes(self, tmp_path):
        _touch(tmp_path, "a/b/c.py")
        (f,) = list(iter_files(tmp_path))
        assert f.relative_path == "a/b/c.py"
        assert "\\" not in f.relative_path

    def test_size_populated(self, tmp_path):
        _touch(tmp_path, "a.py", content="hello\n")
        (f,) = list(iter_files(tmp_path))
        assert f.size == len(b"hello\n")
