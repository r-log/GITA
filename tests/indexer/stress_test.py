"""
Stress test for the indexer pipeline — parsers, code map, and graph construction.

Runs locally with zero external dependencies (no DB, no GitHub API, no LLM).
Tests against synthetic repos of configurable size or real local repos.

Usage:
    # Synthetic repos (default sizes: 100, 500, 2000 files)
    python -m tests.indexer.stress_test

    # Custom size
    python -m tests.indexer.stress_test --synthetic 5000

    # Real local repo (cloned repo directory)
    python -m tests.indexer.stress_test --repo /path/to/local/repo

    # Both
    python -m tests.indexer.stress_test --synthetic 1000 --repo /path/to/repo

    # Quick smoke test
    python -m tests.indexer.stress_test --synthetic 50
"""

from __future__ import annotations

import argparse
import os
import random
import string
import sys
import time
import tracemalloc
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Set env vars before any src imports (Settings singleton)
os.environ.setdefault("GITHUB_APP_ID", "12345")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("OPENROUTER_API_KEY", "test-openrouter-key")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test_db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("GITHUB_APP_PRIVATE_KEY_PATH", "tests/fixtures/fake-key.pem")
os.environ.setdefault("ENVIRONMENT", "testing")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from src.indexer.parsers import parse_file, FileIndex, detect_language, EXTENSION_MAP
from src.indexer.code_map import generate_code_map
from src.indexer.graph_builder import _build_nodes_for_file, _build_edges_for_file, _build_module_lookup, _build_name_lookup
from src.indexer.downloader import _should_skip


# ── Result Tracking ──────────────────────────────────────────────


@dataclass
class StageResult:
    name: str
    duration_s: float
    peak_memory_mb: float
    items_processed: int
    output_size: int = 0  # bytes, for code map output
    extra: dict = field(default_factory=dict)


@dataclass
class StressResult:
    label: str
    total_files: int
    indexable_files: int
    stages: list[StageResult] = field(default_factory=list)
    language_breakdown: dict[str, int] = field(default_factory=dict)
    parse_errors: int = 0
    total_duration_s: float = 0.0
    peak_memory_mb: float = 0.0


def _measure(func, *args, **kwargs):
    """Run func, return (result, duration_seconds, peak_memory_mb)."""
    tracemalloc.start()
    start = time.perf_counter()
    result = func(*args, **kwargs)
    duration = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return result, duration, peak / (1024 * 1024)


# ── Synthetic Repo Generator ─────────────────────────────────────


# Realistic distribution of languages in a full-stack monorepo
LANGUAGE_WEIGHTS = {
    "python": 25,
    "javascript": 15,
    "typescript": 15,
    "go": 8,
    "java": 8,
    "rust": 5,
    "json": 8,
    "yaml": 5,
    "html": 3,
    "css": 3,
    "sql": 2,
    "markdown": 2,
    "shell": 1,
}

# Reverse lookup: language -> extension
LANG_TO_EXT = {}
for ext, lang in EXTENSION_MAP.items():
    if lang not in LANG_TO_EXT:
        LANG_TO_EXT[lang] = ext

# Manual overrides for languages not in EXTENSION_MAP reverse
LANG_TO_EXT.setdefault("python", ".py")
LANG_TO_EXT.setdefault("javascript", ".js")
LANG_TO_EXT.setdefault("typescript", ".ts")


def _rand_name(prefix: str = "", length: int = 8) -> str:
    suffix = "".join(random.choices(string.ascii_lowercase, k=length))
    return f"{prefix}{suffix}" if prefix else suffix


def _rand_class_name() -> str:
    return "".join(random.choices(string.ascii_uppercase, k=1)) + _rand_name(length=7)


def _generate_python_file(depth: str, idx: int) -> tuple[str, str]:
    """Generate a realistic Python file with classes, functions, imports, routes."""
    classes_count = random.randint(0, 3)
    funcs_count = random.randint(1, 6)
    imports_count = random.randint(2, 8)

    lines = []

    # Imports
    stdlib = ["os", "sys", "json", "asyncio", "hashlib", "pathlib", "typing", "datetime", "re"]
    third_party = ["flask", "sqlalchemy", "pydantic", "structlog", "httpx", "celery", "redis"]
    internal = [f"src.{_rand_name()}.{_rand_name()}" for _ in range(3)]
    all_imports = stdlib + third_party + internal
    for imp in random.sample(all_imports, min(imports_count, len(all_imports))):
        if "." in imp:
            lines.append(f"from {imp} import {_rand_class_name()}")
        else:
            lines.append(f"import {imp}")

    lines.append("")

    # Constants
    for _ in range(random.randint(0, 3)):
        const_name = _rand_name().upper()
        lines.append(f'{const_name} = "{_rand_name()}"')

    lines.append("")

    # Classes
    bases = ["Base", "BaseModel", "BaseService", "ABC", ""]
    for _ in range(classes_count):
        cls_name = _rand_class_name()
        base = random.choice(bases)
        base_str = f"({base})" if base else ""
        lines.append(f"class {cls_name}{base_str}:")

        # Fields
        for _ in range(random.randint(2, 6)):
            lines.append(f"    {_rand_name()}: str")

        lines.append("")

        # Methods
        for _ in range(random.randint(1, 5)):
            is_async = random.choice([True, False])
            method_name = _rand_name()
            args = ", ".join(["self"] + [_rand_name() for _ in range(random.randint(0, 3))])
            prefix = "async " if is_async else ""
            lines.append(f"    {prefix}def {method_name}({args}):")
            lines.append(f'        """{_rand_name()} operation."""')
            lines.append(f"        pass")
            lines.append("")

    # Top-level functions
    is_route_file = "route" in depth or "api" in depth or random.random() < 0.15
    decorators = ["app.get", "app.post", "app.put", "app.delete", "router.get", "router.post"]
    for i in range(funcs_count):
        is_async = random.choice([True, False])
        func_name = _rand_name()
        args = ", ".join([_rand_name() for _ in range(random.randint(0, 4))])
        prefix = "async " if is_async else ""

        if is_route_file and i < 3:
            dec = random.choice(decorators)
            path = f"/api/{_rand_name()}/{_rand_name()}"
            lines.append(f'@{dec}("{path}")')

        lines.append(f"{prefix}def {func_name}({args}):")
        lines.append(f"    # TODO: implement {func_name}")
        lines.append(f"    pass")
        lines.append("")

    content = "\n".join(lines)
    path = f"{depth}/{_rand_name()}_{idx}.py"
    return path, content


def _generate_js_file(depth: str, idx: int, ts: bool = False) -> tuple[str, str]:
    """Generate a realistic JS/TS file."""
    lines = []
    ext = ".ts" if ts else ".js"

    # Imports
    libs = ["react", "express", "axios", "lodash", "moment", "next", "vue"]
    for _ in range(random.randint(1, 5)):
        lib = random.choice(libs)
        lines.append(f"import {{ {_rand_class_name()} }} from '{lib}';")

    for _ in range(random.randint(0, 3)):
        lines.append(f"import {{ {_rand_name()} }} from './{_rand_name()}';")

    lines.append("")

    # Classes
    for _ in range(random.randint(0, 2)):
        cls_name = _rand_class_name()
        base = random.choice(["BaseClient", "Component", "Service", ""])
        ext_str = f" extends {base}" if base else ""
        lines.append(f"export class {cls_name}{ext_str} {{")
        for _ in range(random.randint(1, 4)):
            lines.append(f"  {_rand_name()}() {{ return null; }}")
        lines.append("}")
        lines.append("")

    # Functions
    is_route = "route" in depth or "api" in depth or random.random() < 0.1
    for i in range(random.randint(1, 5)):
        func_name = _rand_name()
        if is_route and i < 2:
            method = random.choice(["get", "post", "put", "delete"])
            path = f"/api/{_rand_name()}"
            lines.append(f"app.{method}('{path}', {func_name});")

        is_async = random.choice([True, False])
        prefix = "async " if is_async else ""
        lines.append(f"export {prefix}function {func_name}() {{")
        lines.append(f"  // TODO: implement")
        lines.append(f"}}")
        lines.append("")

    # Components (if React-like)
    if random.random() < 0.3:
        comp_name = _rand_class_name()
        lines.append(f"function {comp_name}(props) {{")
        lines.append(f"  return <div>{comp_name}</div>;")
        lines.append(f"}}")

    content = "\n".join(lines)
    path = f"{depth}/{_rand_name()}_{idx}{ext}"
    return path, content


def _generate_go_file(depth: str, idx: int) -> tuple[str, str]:
    """Generate a realistic Go file."""
    lines = [f'package {_rand_name(length=4)}', ""]

    # Imports
    lines.append("import (")
    go_imports = ["fmt", "net/http", "encoding/json", "context", "sync", "io", "strings",
                  "github.com/gin-gonic/gin", "gorm.io/gorm", "go.uber.org/zap"]
    for imp in random.sample(go_imports, min(random.randint(2, 5), len(go_imports))):
        lines.append(f'    "{imp}"')
    lines.append(")")
    lines.append("")

    # Structs
    for _ in range(random.randint(1, 3)):
        name = _rand_class_name()
        lines.append(f"type {name} struct {{")
        for _ in range(random.randint(2, 6)):
            lines.append(f"    {_rand_class_name()} string")
        lines.append("}")
        lines.append("")

        # Methods
        for _ in range(random.randint(0, 3)):
            method = _rand_name()
            lines.append(f"func (s *{name}) {method}() error {{")
            lines.append(f"    return nil")
            lines.append("}")
            lines.append("")

    # Functions
    for _ in range(random.randint(1, 4)):
        func = _rand_name()
        lines.append(f"func {func}(ctx context.Context) error {{")
        lines.append(f"    return nil")
        lines.append("}")
        lines.append("")

    # Routes
    if random.random() < 0.2:
        lines.append("func SetupRoutes(r *gin.Engine) {")
        for _ in range(random.randint(2, 5)):
            method = random.choice(["GET", "POST", "PUT", "DELETE"])
            path = f"/api/{_rand_name()}"
            handler = _rand_name()
            lines.append(f'    r.{method}("{path}", {handler})')
        lines.append("}")

    content = "\n".join(lines)
    path = f"{depth}/{_rand_name()}_{idx}.go"
    return path, content


def _generate_java_file(depth: str, idx: int) -> tuple[str, str]:
    """Generate a realistic Java file."""
    pkg = depth.replace("/", ".")
    lines = [f"package {pkg};", ""]

    # Imports
    java_imports = [
        "java.util.List", "java.util.Map", "java.util.Optional",
        "javax.persistence.Entity", "javax.persistence.Id",
        "org.springframework.web.bind.annotation.*",
        "org.springframework.stereotype.Service",
    ]
    for imp in random.sample(java_imports, min(random.randint(2, 5), len(java_imports))):
        lines.append(f"import {imp};")
    lines.append("")

    # Class
    cls_name = _rand_class_name()
    base = random.choice(["", " extends BaseEntity", " implements Serializable"])
    is_controller = random.random() < 0.2
    if is_controller:
        lines.append("@RestController")
        lines.append(f'@RequestMapping("/api/{_rand_name()}")')

    lines.append(f"public class {cls_name}{base} {{")

    # Fields
    for _ in range(random.randint(2, 6)):
        lines.append(f"    private String {_rand_name()};")

    lines.append("")

    # Methods
    methods_list = ["GET", "POST", "PUT", "DELETE"]
    for _ in range(random.randint(2, 6)):
        method_name = _rand_name()
        if is_controller and random.random() < 0.5:
            http = random.choice(methods_list)
            lines.append(f'    @{http.capitalize()}Mapping("/{_rand_name()}")')
        lines.append(f"    public String {method_name}() {{")
        lines.append(f'        return "ok";')
        lines.append(f"    }}")
        lines.append("")

    lines.append("}")

    content = "\n".join(lines)
    path = f"{depth}/{cls_name}_{idx}.java"
    return path, content


def _generate_rust_file(depth: str, idx: int) -> tuple[str, str]:
    """Generate a realistic Rust file."""
    lines = []

    # Use statements
    rust_imports = [
        "std::collections::HashMap", "std::sync::Arc", "serde::{Serialize, Deserialize}",
        "tokio::sync::Mutex", "actix_web::{web, HttpResponse}",
        "axum::{Router, routing::get}",
    ]
    for imp in random.sample(rust_imports, min(random.randint(1, 4), len(rust_imports))):
        lines.append(f"use {imp};")
    lines.append("")

    # Structs
    for _ in range(random.randint(1, 3)):
        name = _rand_class_name()
        lines.append("#[derive(Debug, Clone, Serialize, Deserialize)]")
        lines.append(f"pub struct {name} {{")
        for _ in range(random.randint(2, 5)):
            lines.append(f"    pub {_rand_name()}: String,")
        lines.append("}")
        lines.append("")

        # impl
        lines.append(f"impl {name} {{")
        for _ in range(random.randint(1, 3)):
            fn_name = _rand_name()
            lines.append(f"    pub fn {fn_name}(&self) -> &str {{")
            lines.append(f'        "ok"')
            lines.append(f"    }}")
        lines.append("}")
        lines.append("")

    # Functions
    for _ in range(random.randint(1, 3)):
        fn_name = _rand_name()
        is_async = random.choice([True, False])
        prefix = "async " if is_async else ""
        lines.append(f"pub {prefix}fn {fn_name}() -> Result<(), Box<dyn std::error::Error>> {{")
        lines.append(f"    Ok(())")
        lines.append(f"}}")
        lines.append("")

    content = "\n".join(lines)
    path = f"{depth}/{_rand_name()}_{idx}.rs"
    return path, content


def _generate_config_file(depth: str, idx: int, ext: str) -> tuple[str, str]:
    """Generate a config file (JSON/YAML)."""
    if ext == ".json":
        import json
        data = {
            "name": _rand_name(),
            "version": f"{random.randint(1,5)}.{random.randint(0,9)}.{random.randint(0,9)}",
            "scripts": {_rand_name(): f"node {_rand_name()}.js" for _ in range(random.randint(1, 5))},
            "dependencies": {_rand_name(): f"^{random.randint(1,9)}.0.0" for _ in range(random.randint(3, 15))},
            "devDependencies": {_rand_name(): f"^{random.randint(1,9)}.0.0" for _ in range(random.randint(2, 8))},
        }
        content = json.dumps(data, indent=2)
        if idx == 0:
            path = f"{depth}/package.json"
        else:
            path = f"{depth}/{_rand_name()}_{idx}.json"
    else:
        # YAML
        lines = []
        for _ in range(random.randint(3, 10)):
            key = _rand_name()
            lines.append(f"{key}:")
            for _ in range(random.randint(1, 4)):
                lines.append(f"  {_rand_name()}: {_rand_name()}")
        content = "\n".join(lines)
        path = f"{depth}/{_rand_name()}_{idx}.yml"

    return path, content


def _generate_other_file(depth: str, idx: int, lang: str) -> tuple[str, str]:
    """Generate HTML/CSS/SQL/Markdown/Shell files."""
    size = random.randint(20, 200)
    lines = [f"# {lang} file {idx}"]
    for i in range(size):
        lines.append(f"// Line {i}: {_rand_name(length=30)}")
        if random.random() < 0.02:
            lines.append(f"// TODO: fix {_rand_name()}")

    ext = LANG_TO_EXT.get(lang, ".txt")
    content = "\n".join(lines)
    path = f"{depth}/{_rand_name()}_{idx}{ext}"
    return path, content


def generate_synthetic_repo(num_files: int) -> dict[str, str]:
    """
    Generate a synthetic repo with realistic structure and language distribution.
    Returns {file_path: content}.
    """
    random.seed(42)  # Reproducible

    # Build weighted language list
    languages = []
    for lang, weight in LANGUAGE_WEIGHTS.items():
        languages.extend([lang] * weight)

    # Realistic directory structure
    dirs = [
        "src/api", "src/api/routes", "src/api/middleware",
        "src/core", "src/core/config",
        "src/models", "src/models/entities",
        "src/services", "src/services/auth", "src/services/payments",
        "src/workers", "src/workers/jobs",
        "src/utils", "src/utils/helpers",
        "lib", "lib/client", "lib/server",
        "frontend/src", "frontend/src/components", "frontend/src/pages",
        "frontend/src/hooks", "frontend/src/utils",
        "pkg/handlers", "pkg/middleware", "pkg/models",
        "internal/auth", "internal/db", "internal/queue",
        "tests", "tests/unit", "tests/integration",
        "scripts", "config", "deploy",
    ]

    files: dict[str, str] = {}

    for i in range(num_files):
        lang = random.choice(languages)
        depth = random.choice(dirs)

        if lang == "python":
            path, content = _generate_python_file(depth, i)
        elif lang == "javascript":
            path, content = _generate_js_file(depth, i, ts=False)
        elif lang == "typescript":
            path, content = _generate_js_file(depth, i, ts=True)
        elif lang == "go":
            path, content = _generate_go_file(depth, i)
        elif lang == "java":
            path, content = _generate_java_file(depth, i)
        elif lang == "rust":
            path, content = _generate_rust_file(depth, i)
        elif lang == "json":
            path, content = _generate_config_file(depth, i, ".json")
        elif lang == "yaml":
            path, content = _generate_config_file(depth, i, ".yml")
        else:
            path, content = _generate_other_file(depth, i, lang)

        files[path] = content

    return files


# ── Local Repo Reader ────────────────────────────────────────────


def read_local_repo(repo_path: str) -> dict[str, str]:
    """
    Read all indexable files from a local directory.
    Applies the same filtering as the real downloader.
    """
    repo_root = Path(repo_path).resolve()
    if not repo_root.is_dir():
        print(f"Error: {repo_path} is not a directory")
        sys.exit(1)

    files: dict[str, str] = {}
    skipped = 0

    for file_path in repo_root.rglob("*"):
        if not file_path.is_file():
            continue

        # Convert to relative path with forward slashes
        rel_path = str(file_path.relative_to(repo_root)).replace("\\", "/")

        # Apply downloader filtering
        size = file_path.stat().st_size
        if _should_skip(rel_path, size):
            skipped += 1
            continue

        # Skip files we can't read as text
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except (OSError, PermissionError):
            skipped += 1
            continue

        files[rel_path] = content

    print(f"  Read {len(files)} files from {repo_path} (skipped {skipped})")
    return files


# ── Stress Test Pipeline ─────────────────────────────────────────


def run_stress_test(files: dict[str, str], label: str) -> StressResult:
    """Run the full indexer pipeline on a set of files and measure performance."""
    result = StressResult(
        label=label,
        total_files=len(files),
        indexable_files=len(files),
    )

    overall_start = time.perf_counter()
    overall_peak = 0.0

    # ── Stage 1: Parse all files ─────────────────────────────────
    print(f"\n  [1/4] Parsing {len(files)} files...")
    tracemalloc.start()
    parse_start = time.perf_counter()

    parsed: list[FileIndex] = []
    errors = 0
    lang_counts: dict[str, int] = defaultdict(int)

    for path, content in files.items():
        fi = parse_file(content, path)
        parsed.append(fi)
        lang_counts[fi.language] += 1
        if fi.structure.get("parse_error"):
            errors += 1

    parse_duration = time.perf_counter() - parse_start
    _, parse_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    result.parse_errors = errors
    result.language_breakdown = dict(lang_counts)
    parse_peak_mb = parse_peak / (1024 * 1024)
    overall_peak = max(overall_peak, parse_peak_mb)

    # Count extracted symbols
    total_classes = sum(len(fi.structure.get("classes", [])) for fi in parsed)
    total_functions = sum(len(fi.structure.get("functions", [])) for fi in parsed)
    total_routes = sum(len(fi.structure.get("routes", [])) for fi in parsed)
    total_imports = sum(len(fi.structure.get("imports", [])) for fi in parsed)

    result.stages.append(StageResult(
        name="parse",
        duration_s=parse_duration,
        peak_memory_mb=parse_peak_mb,
        items_processed=len(parsed),
        extra={
            "classes": total_classes,
            "functions": total_functions,
            "routes": total_routes,
            "imports": total_imports,
            "errors": errors,
            "files_per_sec": len(parsed) / parse_duration if parse_duration > 0 else 0,
        },
    ))

    print(f"         {len(parsed)} files in {parse_duration:.2f}s "
          f"({len(parsed)/parse_duration:.0f} files/sec), "
          f"peak {parse_peak_mb:.1f}MB")
    print(f"         Symbols: {total_classes} classes, {total_functions} functions, "
          f"{total_routes} routes, {total_imports} imports")

    # ── Stage 2: Code map generation ─────────────────────────────
    print(f"  [2/4] Generating code map...")
    records_for_map = [
        {
            "file_path": fi.file_path,
            "language": fi.language,
            "line_count": fi.line_count,
            "structure": fi.structure,
        }
        for fi in parsed
    ]

    tracemalloc.start()
    map_start = time.perf_counter()
    code_map = generate_code_map(records_for_map, project_name=label)
    map_duration = time.perf_counter() - map_start
    _, map_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    map_peak_mb = map_peak / (1024 * 1024)
    overall_peak = max(overall_peak, map_peak_mb)
    map_size = len(code_map.encode("utf-8"))

    result.stages.append(StageResult(
        name="code_map",
        duration_s=map_duration,
        peak_memory_mb=map_peak_mb,
        items_processed=len(records_for_map),
        output_size=map_size,
    ))

    print(f"         {map_size:,} bytes in {map_duration:.3f}s, peak {map_peak_mb:.1f}MB")

    # ── Stage 3: Graph node construction ─────────────────────────
    print(f"  [3/4] Building graph nodes...")
    tracemalloc.start()
    node_start = time.perf_counter()

    repo_id = 1  # dummy
    all_nodes = []
    node_lookup: dict[str, int] = {}
    file_path_to_node: dict[str, int] = {}
    fake_id = 1

    for fi in parsed:
        nodes = _build_nodes_for_file(repo_id, fi)
        for node in nodes:
            # Simulate DB ID assignment
            node.id = fake_id
            node_lookup[node.qualified_name] = node.id
            if node.node_type == "file":
                file_path_to_node[node.file_path] = node.id
            fake_id += 1
        all_nodes.extend(nodes)

    node_duration = time.perf_counter() - node_start
    _, node_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    node_peak_mb = node_peak / (1024 * 1024)
    overall_peak = max(overall_peak, node_peak_mb)

    # Count node types
    node_types = defaultdict(int)
    for node in all_nodes:
        node_types[node.node_type] += 1

    result.stages.append(StageResult(
        name="graph_nodes",
        duration_s=node_duration,
        peak_memory_mb=node_peak_mb,
        items_processed=len(all_nodes),
        extra=dict(node_types),
    ))

    print(f"         {len(all_nodes):,} nodes in {node_duration:.3f}s, peak {node_peak_mb:.1f}MB")
    print(f"         Types: {dict(node_types)}")

    # ── Stage 4: Graph edge construction ─────────────────────────
    print(f"  [4/4] Building graph edges...")
    tracemalloc.start()
    edge_start = time.perf_counter()

    module_lookup = _build_module_lookup(parsed, node_lookup)
    name_lookup = _build_name_lookup(node_lookup)
    all_edges = []
    for fi in parsed:
        edges = _build_edges_for_file(repo_id, fi, node_lookup, module_lookup, file_path_to_node, name_lookup)
        all_edges.extend(edges)

    edge_duration = time.perf_counter() - edge_start
    _, edge_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    edge_peak_mb = edge_peak / (1024 * 1024)
    overall_peak = max(overall_peak, edge_peak_mb)

    # Count edge types
    edge_types = defaultdict(int)
    for edge in all_edges:
        edge_types[edge.edge_type] += 1

    result.stages.append(StageResult(
        name="graph_edges",
        duration_s=edge_duration,
        peak_memory_mb=edge_peak_mb,
        items_processed=len(all_edges),
        extra=dict(edge_types),
    ))

    print(f"         {len(all_edges):,} edges in {edge_duration:.3f}s, peak {edge_peak_mb:.1f}MB")
    print(f"         Types: {dict(edge_types)}")

    result.total_duration_s = time.perf_counter() - overall_start
    result.peak_memory_mb = overall_peak

    return result


# ── Reporting ────────────────────────────────────────────────────


def print_summary(results: list[StressResult]):
    """Print a comparison summary table."""
    print("\n" + "=" * 80)
    print("STRESS TEST SUMMARY")
    print("=" * 80)

    # Header
    header = f"{'Label':<30} {'Files':>6} {'Parse':>8} {'Map':>8} {'Nodes':>8} {'Edges':>8} {'Total':>8} {'Peak MB':>8}"
    print(header)
    print("-" * 80)

    for r in results:
        stages = {s.name: s for s in r.stages}
        parse_s = stages.get("parse")
        codemap_s = stages.get("code_map")
        nodes_s = stages.get("graph_nodes")
        edges_s = stages.get("graph_edges")

        parse_str = f"{parse_s.duration_s:>7.2f}s" if parse_s else "     N/A"
        map_str = f"{codemap_s.duration_s:>7.3f}s" if codemap_s else "     N/A"
        nodes_str = f"{nodes_s.items_processed:>8,}" if nodes_s else "     N/A"
        edges_str = f"{edges_s.items_processed:>8,}" if edges_s else "     N/A"

        print(f"{r.label:<30} {r.total_files:>6} {parse_str} {map_str} "
              f"{nodes_str} {edges_str} {r.total_duration_s:>7.2f}s {r.peak_memory_mb:>7.1f}")

    print("-" * 80)

    # Detailed per-result breakdown
    for r in results:
        print(f"\n--- {r.label} ---")
        print(f"  Files: {r.total_files} | Parse errors: {r.parse_errors}")
        print(f"  Languages: {r.language_breakdown}")

        for s in r.stages:
            extras = ""
            if s.extra:
                extras = " | " + ", ".join(f"{k}={v}" for k, v in s.extra.items())
            if s.output_size:
                extras += f" | output={s.output_size:,}B"

            throughput = ""
            if s.name == "parse" and s.extra.get("files_per_sec"):
                throughput = f" ({s.extra['files_per_sec']:.0f} files/sec)"

            print(f"  {s.name:<15} {s.duration_s:>7.3f}s  {s.peak_memory_mb:>6.1f}MB  "
                  f"{s.items_processed:>8,} items{throughput}{extras}")

    # Scaling analysis
    if len(results) >= 2:
        print(f"\n--- Scaling Analysis ---")
        sorted_results = sorted(results, key=lambda r: r.total_files)
        base = sorted_results[0]
        base_parse = next((s for s in base.stages if s.name == "parse"), None)
        if base_parse and base_parse.duration_s > 0:
            for r in sorted_results[1:]:
                r_parse = next((s for s in r.stages if s.name == "parse"), None)
                if r_parse:
                    file_ratio = r.total_files / base.total_files
                    time_ratio = r_parse.duration_s / base_parse.duration_s
                    print(f"  {base.total_files} -> {r.total_files} files "
                          f"({file_ratio:.1f}x): parse time {time_ratio:.1f}x "
                          f"({'linear' if 0.8 < time_ratio/file_ratio < 1.3 else 'non-linear'})")

    print()


# ── Main ─────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Stress test the code indexer pipeline")
    parser.add_argument(
        "--synthetic", type=int, nargs="*", default=None,
        help="Generate synthetic repos with N files. Multiple values for comparison. "
             "Default if no args: 100, 500, 2000",
    )
    parser.add_argument(
        "--repo", type=str, nargs="*", default=None,
        help="Path(s) to local repo directories to index",
    )
    args = parser.parse_args()

    # Default: run synthetic benchmarks at 3 scales
    if args.synthetic is None and args.repo is None:
        args.synthetic = [100, 500, 2000]
    elif args.synthetic is not None and len(args.synthetic) == 0:
        args.synthetic = [100, 500, 2000]

    results: list[StressResult] = []

    # Synthetic repos
    if args.synthetic:
        for size in args.synthetic:
            print(f"\n{'='*60}")
            print(f"Synthetic repo: {size} files")
            print(f"{'='*60}")

            gen_start = time.perf_counter()
            files = generate_synthetic_repo(size)
            gen_dur = time.perf_counter() - gen_start
            print(f"  Generated {len(files)} files in {gen_dur:.2f}s")

            total_bytes = sum(len(c.encode("utf-8")) for c in files.values())
            print(f"  Total size: {total_bytes / (1024*1024):.1f}MB")

            result = run_stress_test(files, f"synthetic-{size}")
            results.append(result)

    # Real repos
    if args.repo:
        for repo_path in args.repo:
            print(f"\n{'='*60}")
            print(f"Local repo: {repo_path}")
            print(f"{'='*60}")

            files = read_local_repo(repo_path)
            if not files:
                print(f"  No indexable files found, skipping.")
                continue

            total_bytes = sum(len(c.encode("utf-8")) for c in files.values())
            print(f"  Total size: {total_bytes / (1024*1024):.1f}MB")

            label = Path(repo_path).resolve().name
            result = run_stress_test(files, label)
            results.append(result)

    # Summary
    print_summary(results)


if __name__ == "__main__":
    main()
