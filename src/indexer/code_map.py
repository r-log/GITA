"""
Code Map Generator — creates a compressed text representation of a project
from CodeIndex records. This is what the LLM reads instead of raw source files.

Produces ~2-10KB of structured markdown that captures the full project shape:
routes, models, services, components, configs, gaps.
"""

from collections import defaultdict

import structlog

log = structlog.get_logger()


def generate_code_map(records: list[dict], project_name: str = "") -> str:
    """
    Generate a compressed code map from CodeIndex records.

    Args:
        records: list of dicts with file_path, language, line_count, structure
        project_name: optional project name for the header

    Returns:
        Markdown text (~2-10KB) summarizing the entire project.
    """
    if not records:
        return "# Empty Repository\nNo indexable source files found."

    # Classify files
    by_language = defaultdict(list)
    all_routes = []
    all_models = []
    all_functions = []
    all_components = []
    all_todos = []
    all_imports = set()
    dependencies = {}
    services = []
    total_lines = 0

    for rec in records:
        path = rec["file_path"]
        lang = rec["language"]
        struct = rec.get("structure", {})
        lines = rec.get("line_count", 0)
        total_lines += lines

        by_language[lang].append(path)

        # Collect routes
        for route in struct.get("routes", []):
            all_routes.append({
                "method": route.get("method", "ANY"),
                "path": route.get("path", ""),
                "handler": route.get("handler", ""),
                "file": path,
            })

        # Collect classes (potential models/services)
        for cls in struct.get("classes", []):
            cls_info = {
                "name": cls.get("name", ""),
                "bases": cls.get("bases", []),
                "methods": cls.get("methods", []),
                "fields": cls.get("fields", []),
                "file": path,
            }
            # Classify: is it a model/schema or a service?
            lower_path = path.lower()
            bases_str = " ".join(cls.get("bases", [])).lower()
            if any(kw in lower_path for kw in ("model", "schema", "entity")) or \
               any(kw in bases_str for kw in ("base", "model", "db.", "document")):
                all_models.append(cls_info)
            elif any(kw in lower_path for kw in ("service", "manager", "handler", "controller")):
                services.append(cls_info)

        # Collect standalone functions
        for fn in struct.get("functions", []):
            fn_info = {
                "name": fn.get("name", ""),
                "args": fn.get("args", []),
                "decorators": fn.get("decorators", []),
                "is_async": fn.get("is_async", False),
                "file": path,
            }
            # Services: functions in service directories
            if any(kw in path.lower() for kw in ("service", "handler", "controller", "api")):
                services.append(fn_info)
            all_functions.append(fn_info)

        # Collect components (JS/Vue/Svelte)
        for comp in struct.get("components", []):
            all_components.append({"name": comp, "file": path})

        # Collect exports
        for exp in struct.get("exports", []):
            all_components.append({"name": exp, "file": path})

        # Collect TODOs
        for todo in struct.get("todos", []):
            all_todos.append({"file": path, **todo})

        # Collect imports for dependency analysis
        for imp in struct.get("imports", []):
            all_imports.add(imp)

        # Collect dependencies from config files
        for dep_key in ("dependencies", "dev_dependencies"):
            if dep_key in struct:
                dependencies[dep_key] = struct[dep_key]

    # ── Build the code map ──────────────────────────────────────

    sections = []

    # Header
    lang_summary = ", ".join(
        f"{lang} ({len(files)})" for lang, files in sorted(by_language.items(), key=lambda x: -len(x[1]))
        if lang != "other"
    )
    sections.append(f"# {project_name or 'Project'}")
    sections.append(f"**Languages:** {lang_summary}")
    sections.append(f"**Total files:** {len(records)} | **Total lines:** {total_lines:,}")

    # Dependencies
    if dependencies:
        sections.append("\n## Dependencies")
        for dep_type, deps in dependencies.items():
            if deps:
                label = "Production" if dep_type == "dependencies" else "Development"
                sections.append(f"**{label}:** {', '.join(deps[:20])}")
                if len(deps) > 20:
                    sections.append(f"  +{len(deps) - 20} more")

    # API Routes
    if all_routes:
        sections.append("\n## API Routes")
        # Group by file
        routes_by_file = defaultdict(list)
        for r in all_routes:
            routes_by_file[r["file"]].append(r)
        for file, routes in sorted(routes_by_file.items()):
            sections.append(f"**{file}:**")
            for r in routes:
                sections.append(f"  - {r['method']} {r['path']} → {r['handler']}()")

    # Models/Schemas
    if all_models:
        sections.append("\n## Models & Schemas")
        for m in all_models:
            fields_str = ", ".join(m["fields"][:10]) if m["fields"] else ""
            methods_str = ", ".join(m["methods"][:8]) if m["methods"] else ""
            bases_str = f" ({', '.join(m['bases'])})" if m["bases"] else ""
            sections.append(f"- **{m['name']}**{bases_str} [{m['file']}]")
            if fields_str:
                sections.append(f"  Fields: {fields_str}")
            if methods_str:
                sections.append(f"  Methods: {methods_str}")

    # Services/Business Logic
    if services:
        sections.append("\n## Services & Business Logic")
        svc_by_file = defaultdict(list)
        for s in services:
            svc_by_file[s["file"]].append(s)
        for file, svcs in sorted(svc_by_file.items()):
            names = []
            for s in svcs:
                name = s.get("name", "")
                if "methods" in s:  # class
                    names.append(f"{name} (class, {len(s['methods'])} methods)")
                else:  # function
                    args = ", ".join(s.get("args", []))
                    prefix = "async " if s.get("is_async") else ""
                    names.append(f"{prefix}{name}({args})")
            sections.append(f"**{file}:** {'; '.join(names)}")

    # Frontend Components
    if all_components:
        sections.append("\n## Frontend Components")
        comp_by_file = defaultdict(list)
        for c in all_components:
            comp_by_file[c["file"]].append(c["name"])
        for file, comps in sorted(comp_by_file.items()):
            sections.append(f"- {file}: {', '.join(comps)}")

    # File Structure (condensed)
    sections.append("\n## File Structure")
    dirs = defaultdict(list)
    for rec in records:
        parts = rec["file_path"].split("/")
        if len(parts) > 1:
            dir_path = "/".join(parts[:-1])
        else:
            dir_path = "."
        dirs[dir_path].append(parts[-1])

    for dir_path in sorted(dirs.keys()):
        files = dirs[dir_path]
        if len(files) <= 5:
            sections.append(f"  {dir_path}/ → {', '.join(files)}")
        else:
            sections.append(f"  {dir_path}/ → {len(files)} files ({', '.join(files[:3])}, ...)")

    # Gaps & TODOs
    if all_todos:
        sections.append(f"\n## TODOs & FIXMEs ({len(all_todos)})")
        for todo in all_todos[:15]:
            sections.append(f"- [{todo['file']}:{todo.get('line', '?')}] {todo['text'][:120]}")
        if len(all_todos) > 15:
            sections.append(f"  +{len(all_todos) - 15} more")

    # Quality gaps
    gaps = _detect_gaps(records, by_language, all_routes, all_models)
    if gaps:
        sections.append("\n## Detected Gaps")
        for gap in gaps:
            sections.append(f"- {gap}")

    return "\n".join(sections)


def _detect_gaps(records, by_language, routes, models) -> list[str]:
    """Detect common project gaps from the index."""
    gaps = []
    all_paths = {r["file_path"] for r in records}
    all_paths_lower = {p.lower() for p in all_paths}

    # No tests
    test_files = [p for p in all_paths if "test" in p.lower() or "spec" in p.lower()]
    if not test_files:
        gaps.append("No test files found")
    elif len(test_files) < len(routes) // 2:
        gaps.append(f"Low test coverage: {len(test_files)} test files for {len(routes)} routes")

    # No Dockerfile
    if not any("dockerfile" in p for p in all_paths_lower):
        gaps.append("No Dockerfile found")

    # No CI/CD
    if not any(".github/workflows" in p or ".gitlab-ci" in p for p in all_paths_lower):
        gaps.append("No CI/CD configuration found")

    # No README
    if not any(p.lower().endswith("readme.md") or p.lower() == "readme" for p in all_paths):
        gaps.append("No README.md found")

    # No .env.example
    if not any(".env.example" in p or ".env.sample" in p for p in all_paths_lower):
        if any(".env" in p for p in all_paths_lower):
            gaps.append("Has .env but no .env.example template")

    # Models without migrations
    if models and not any("migration" in p.lower() or "alembic" in p.lower() for p in all_paths_lower):
        gaps.append(f"Found {len(models)} models but no database migrations")

    return gaps
