"""
Graph builder — materializes code relationships into queryable graph tables.

Deterministic, zero LLM cost. Runs after file parsing during full/incremental indexing.

Two entry points:
- build_graph_for_repo(): Full rebuild on first install
- update_graph_for_files(): Incremental update on push
"""

from __future__ import annotations

import structlog
from sqlalchemy import delete, select

from src.core.database import async_session
from src.indexer.parsers import FileIndex
from src.models.graph_node import GraphNode
from src.models.graph_edge import GraphEdge
from src.models.file_mapping import FileMapping

log = structlog.get_logger()


# ── Public API ────────────────────────────────────────────────────


async def build_graph_for_repo(repo_id: int, parsed_files: list[FileIndex]) -> dict:
    """
    Full graph build: delete existing graph data for repo, rebuild from parsed files.

    Called from index_repository() after storing CodeIndex records.
    Returns summary dict with counts.
    """
    log.info("graph_build_start", repo_id=repo_id, files=len(parsed_files))

    async with async_session() as session:
        # Delete existing graph data (edges first due to FK)
        await session.execute(
            delete(GraphEdge).where(GraphEdge.repo_id == repo_id)
        )
        await session.execute(
            delete(GraphNode).where(GraphNode.repo_id == repo_id)
        )
        await session.flush()

        # Phase 1: Create all nodes
        node_count = 0
        node_lookup: dict[str, int] = {}  # qualified_name -> node.id
        file_path_to_node: dict[str, int] = {}  # file_path -> file node.id

        for fi in parsed_files:
            nodes = _build_nodes_for_file(repo_id, fi)
            for node in nodes:
                session.add(node)
                await session.flush()  # get the ID
                node_lookup[node.qualified_name] = node.id
                if node.node_type == "file":
                    file_path_to_node[node.file_path] = node.id
            node_count += len(nodes)

        # Build module path lookups for import resolution
        module_lookup = _build_module_lookup(parsed_files, node_lookup)
        name_lookup = _build_name_lookup(node_lookup)

        # Phase 2: Create all edges
        edge_count = 0
        for fi in parsed_files:
            edges = _build_edges_for_file(repo_id, fi, node_lookup, module_lookup, file_path_to_node, name_lookup)
            for edge in edges:
                session.add(edge)
            edge_count += len(edges)

        # Phase 3: Migrate existing file_mappings to graph edges
        mapping_edges = await _migrate_file_mappings(session, repo_id, file_path_to_node)
        edge_count += mapping_edges

        await session.commit()

    log.info("graph_build_complete", repo_id=repo_id, nodes=node_count, edges=edge_count)
    return {"nodes_created": node_count, "edges_created": edge_count}


async def update_graph_for_files(
    repo_id: int,
    changed_files: list[FileIndex],
    removed_paths: set[str],
) -> dict:
    """
    Incremental graph update: remove nodes/edges for changed/removed files, recreate for changed.

    Called from reindex_files() after updating CodeIndex records.
    """
    all_affected_paths = {fi.file_path for fi in changed_files} | removed_paths

    if not all_affected_paths:
        return {"nodes_updated": 0, "edges_updated": 0}

    log.info(
        "graph_update_start",
        repo_id=repo_id,
        changed=len(changed_files),
        removed=len(removed_paths),
    )

    async with async_session() as session:
        # Find existing node IDs for affected files
        result = await session.execute(
            select(GraphNode.id).where(
                GraphNode.repo_id == repo_id,
                GraphNode.file_path.in_(all_affected_paths),
            )
        )
        old_node_ids = [row[0] for row in result.all()]

        # Delete edges involving these nodes (both as source and target)
        if old_node_ids:
            await session.execute(
                delete(GraphEdge).where(
                    GraphEdge.repo_id == repo_id,
                    GraphEdge.source_node_id.in_(old_node_ids),
                )
            )
            await session.execute(
                delete(GraphEdge).where(
                    GraphEdge.repo_id == repo_id,
                    GraphEdge.target_node_id.in_(old_node_ids),
                )
            )
            # Delete the old nodes
            await session.execute(
                delete(GraphNode).where(GraphNode.id.in_(old_node_ids))
            )
            await session.flush()

        # Load existing node lookup for the rest of the repo (for import resolution)
        result = await session.execute(
            select(GraphNode).where(GraphNode.repo_id == repo_id)
        )
        existing_nodes = result.scalars().all()
        node_lookup = {n.qualified_name: n.id for n in existing_nodes}
        file_path_to_node = {
            n.file_path: n.id for n in existing_nodes if n.node_type == "file"
        }

        # Recreate nodes for changed files.
        # Track the new file node IDs separately so we can rebuild ownership
        # edges (belongs_to_issue / belongs_to_milestone) — those were wiped
        # along with the old nodes and won't be recreated by the structural
        # edge pass below.
        node_count = 0
        new_file_nodes: dict[str, int] = {}
        for fi in changed_files:
            nodes = _build_nodes_for_file(repo_id, fi)
            for node in nodes:
                session.add(node)
                await session.flush()
                node_lookup[node.qualified_name] = node.id
                if node.node_type == "file":
                    file_path_to_node[node.file_path] = node.id
                    new_file_nodes[node.file_path] = node.id
            node_count += len(nodes)

        # Rebuild module lookup with all current files
        # We need all parsed files for this, but we only have the changed ones.
        # Use existing nodes + changed files to build a partial lookup.
        module_lookup = _build_module_lookup_from_nodes(existing_nodes)
        module_lookup.update(_build_module_lookup(changed_files, node_lookup))
        name_lookup = _build_name_lookup(node_lookup)

        # Recreate edges for changed files
        edge_count = 0
        for fi in changed_files:
            edges = _build_edges_for_file(
                repo_id, fi, node_lookup, module_lookup, file_path_to_node, name_lookup
            )
            for edge in edges:
                session.add(edge)
            edge_count += len(edges)

        # Re-attach ownership edges for the newly-created file nodes.
        # Without this, every push wipes belongs_to_issue / belongs_to_milestone
        # edges for the changed files and progress_tracker's file_ownership
        # lookup returns empty.
        if new_file_nodes:
            ownership_edges = await _migrate_file_mappings(
                session, repo_id, new_file_nodes
            )
            edge_count += ownership_edges

        await session.commit()

    log.info(
        "graph_update_complete",
        repo_id=repo_id,
        nodes=node_count,
        edges=edge_count,
        ownership_restored=len(new_file_nodes),
    )
    return {"nodes_updated": node_count, "edges_updated": edge_count}


# ── Node Construction ─────────────────────────────────────────────


def _build_nodes_for_file(repo_id: int, fi: FileIndex) -> list[GraphNode]:
    """Create all graph nodes for a single parsed file."""
    nodes = []
    structure = fi.structure or {}

    # File node (always created)
    file_node = GraphNode(
        repo_id=repo_id,
        node_type="file",
        qualified_name=fi.file_path,
        file_path=fi.file_path,
        name=fi.file_path.split("/")[-1],
        language=fi.language,
        extra={"size_bytes": fi.size_bytes, "line_count": fi.line_count},
    )
    nodes.append(file_node)

    # Classes
    for cls in structure.get("classes", []):
        cls_name = cls.get("name", "")
        if not cls_name:
            continue
        qname = f"{fi.file_path}::{cls_name}"
        nodes.append(GraphNode(
            repo_id=repo_id,
            node_type="class",
            qualified_name=qname,
            file_path=fi.file_path,
            name=cls_name,
            language=fi.language,
            line_number=cls.get("line"),
            extra={
                "bases": cls.get("bases", []),
                "decorators": cls.get("decorators", []),
                "fields": cls.get("fields", []),
            },
        ))

        # Methods within classes
        method_details = cls.get("method_details", [])
        if not method_details:
            # Fallback: use the methods list (just names)
            for method_name in cls.get("methods", []):
                mq = f"{fi.file_path}::{cls_name}.{method_name}"
                nodes.append(GraphNode(
                    repo_id=repo_id,
                    node_type="method",
                    qualified_name=mq,
                    file_path=fi.file_path,
                    name=method_name,
                    language=fi.language,
                ))
        else:
            for method in method_details:
                method_name = method.get("name", "")
                if not method_name:
                    continue
                mq = f"{fi.file_path}::{cls_name}.{method_name}"
                nodes.append(GraphNode(
                    repo_id=repo_id,
                    node_type="method",
                    qualified_name=mq,
                    file_path=fi.file_path,
                    name=method_name,
                    language=fi.language,
                    line_number=method.get("line"),
                    extra={
                        "args": method.get("args", []),
                        "decorators": method.get("decorators", []),
                    },
                ))

    # Top-level functions
    seen_func_names: set[str] = set()
    for func in structure.get("functions", []):
        func_name = func.get("name", "")
        if not func_name:
            continue
        # Disambiguate duplicate function names (e.g. inner functions like 'decorated')
        # by appending the line number
        qname = f"{fi.file_path}::{func_name}"
        if qname in seen_func_names:
            line = func.get("line", 0)
            qname = f"{fi.file_path}::{func_name}@L{line}"
        seen_func_names.add(qname)
        nodes.append(GraphNode(
            repo_id=repo_id,
            node_type="function",
            qualified_name=qname,
            file_path=fi.file_path,
            name=func_name,
            language=fi.language,
            line_number=func.get("line"),
            extra={
                "args": func.get("args", []),
                "decorators": func.get("decorators", []),
                "is_async": func.get("is_async", False),
            },
        ))

    # Routes
    for route in structure.get("routes", []):
        handler = route.get("handler", "")
        path = route.get("path", "")
        method = route.get("method", "ANY")
        if not path:
            continue
        qname = f"{fi.file_path}::route:{method}:{path}"
        nodes.append(GraphNode(
            repo_id=repo_id,
            node_type="route",
            qualified_name=qname,
            file_path=fi.file_path,
            name=f"{method} {path}",
            language=fi.language,
            line_number=route.get("line"),
            extra={"handler": handler, "method": method, "path": path},
        ))

    # Components (JS/TS React components)
    for comp_name in structure.get("components", []):
        if not comp_name:
            continue
        qname = f"{fi.file_path}::component:{comp_name}"
        nodes.append(GraphNode(
            repo_id=repo_id,
            node_type="class",  # treat components as class-level entities
            qualified_name=qname,
            file_path=fi.file_path,
            name=comp_name,
            language=fi.language,
            extra={"is_component": True},
        ))

    return nodes


# ── Edge Construction ─────────────────────────────────────────────


def _build_edges_for_file(
    repo_id: int,
    fi: FileIndex,
    node_lookup: dict[str, int],
    module_lookup: dict[str, int],
    file_path_to_node: dict[str, int],
    name_lookup: dict[str, int] | None = None,
) -> list[GraphEdge]:
    """Create all graph edges for a single parsed file."""
    edges = []
    structure = fi.structure or {}

    source_file_node_id = node_lookup.get(fi.file_path)
    if not source_file_node_id:
        return edges

    # Import edges: file -> imported file/symbol
    for imp in structure.get("imports", []):
        target_id = _resolve_import(imp, fi, module_lookup, file_path_to_node)
        if target_id:
            edges.append(GraphEdge(
                repo_id=repo_id,
                source_node_id=source_file_node_id,
                target_node_id=target_id,
                edge_type="imports",
            ))

    # Defines edges: file -> symbols it defines
    for cls in structure.get("classes", []):
        cls_name = cls.get("name", "")
        cls_qname = f"{fi.file_path}::{cls_name}"
        cls_node_id = node_lookup.get(cls_qname)
        if cls_node_id:
            edges.append(GraphEdge(
                repo_id=repo_id,
                source_node_id=source_file_node_id,
                target_node_id=cls_node_id,
                edge_type="defines",
            ))

            # Inheritance edges: class -> base class
            for base in cls.get("bases", []):
                base_id = _resolve_symbol(base, fi, node_lookup, module_lookup, name_lookup)
                if base_id:
                    edges.append(GraphEdge(
                        repo_id=repo_id,
                        source_node_id=cls_node_id,
                        target_node_id=base_id,
                        edge_type="inherits",
                    ))

    for func in structure.get("functions", []):
        func_name = func.get("name", "")
        func_qname = f"{fi.file_path}::{func_name}"
        func_node_id = node_lookup.get(func_qname)
        if func_node_id:
            edges.append(GraphEdge(
                repo_id=repo_id,
                source_node_id=source_file_node_id,
                target_node_id=func_node_id,
                edge_type="defines",
            ))

    return edges


# ── Import Resolution ─────────────────────────────────────────────


def _resolve_import(
    import_str: str,
    fi: FileIndex,
    module_lookup: dict[str, int],
    file_path_to_node: dict[str, int],
) -> int | None:
    """
    Resolve an import string to a graph node ID.

    Tries multiple resolution strategies:
    1. Direct module lookup (Python dotted paths)
    2. Relative path resolution (JS/TS relative imports)
    3. File path pattern matching
    """
    # Strategy 1: Direct module lookup (covers Python `from x.y.z import Foo`)
    if import_str in module_lookup:
        return module_lookup[import_str]

    # Strategy 2: Convert Python dotted path to file path
    # e.g. "src.models.issue" -> "src/models/issue.py"
    if "." in import_str and not import_str.startswith((".", "/")):
        file_candidate = import_str.replace(".", "/") + ".py"
        if file_candidate in file_path_to_node:
            return file_path_to_node[file_candidate]
        # Try as package: src/models/issue/__init__.py
        init_candidate = import_str.replace(".", "/") + "/__init__.py"
        if init_candidate in file_path_to_node:
            return file_path_to_node[init_candidate]

    # Strategy 3: Relative import resolution (JS/TS)
    if import_str.startswith((".", "./")):
        candidates = _resolve_relative_import(import_str, fi.file_path)
        for candidate in candidates:
            if candidate in file_path_to_node:
                return file_path_to_node[candidate]

    # Strategy 4: Try as direct file path
    if import_str in file_path_to_node:
        return file_path_to_node[import_str]

    # Not resolved — external/third-party dependency, skip
    return None


def _resolve_relative_import(import_path: str, source_file: str) -> list[str]:
    """
    Resolve a relative import like './bar' to candidate full file paths.

    Returns a list of candidates for the caller to match against known files.
    """
    source_dir = "/".join(source_file.split("/")[:-1])
    if not source_dir:
        source_dir = "."

    # Normalize the path
    combined = f"{source_dir}/{import_path}"
    parts = []
    for part in combined.split("/"):
        if part == "." or part == "":
            continue
        elif part == "..":
            if parts:
                parts.pop()
        else:
            parts.append(part)

    resolved_base = "/".join(parts)

    # Return candidates with common extensions
    candidates = [resolved_base]
    for ext in (".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.tsx", "/index.js"):
        candidates.append(resolved_base + ext)
    return candidates


def _resolve_symbol(
    symbol_name: str,
    fi: FileIndex,
    node_lookup: dict[str, int],
    module_lookup: dict[str, int],
    name_lookup: dict[str, int] | None = None,
) -> int | None:
    """Resolve a symbol name (like a base class) to a node ID."""
    # Try as qualified name in same file first
    qname = f"{fi.file_path}::{symbol_name}"
    if qname in node_lookup:
        return node_lookup[qname]

    # Try reverse name index (O(1) instead of O(n) scan)
    if name_lookup and symbol_name in name_lookup:
        return name_lookup[symbol_name]

    # Try module lookup
    if symbol_name in module_lookup:
        return module_lookup[symbol_name]

    return None


# ── Lookup Builders ───────────────────────────────────────────────


def _build_name_lookup(node_lookup: dict[str, int]) -> dict[str, int]:
    """
    Build a reverse index: short symbol name -> node ID.

    Extracts the name after '::' from qualified names like 'path/file.py::ClassName'.
    First match wins — this is a best-effort lookup for unqualified symbol resolution.
    """
    lookup: dict[str, int] = {}
    for qualified_name, node_id in node_lookup.items():
        sep = qualified_name.rfind("::")
        if sep != -1:
            short_name = qualified_name[sep + 2:]
            # Don't overwrite — first registered wins (same file preference)
            if short_name not in lookup:
                lookup[short_name] = node_id
    return lookup


def _build_module_lookup(
    parsed_files: list[FileIndex],
    node_lookup: dict[str, int],
) -> dict[str, int]:
    """
    Build a module-path lookup from parsed files.

    Maps Python module paths (e.g. "src.models.issue") and short module names
    to their file node IDs, enabling import resolution.
    """
    lookup: dict[str, int] = {}

    for fi in parsed_files:
        file_node_id = node_lookup.get(fi.file_path)
        if not file_node_id:
            continue

        # Python module path: src/models/issue.py -> src.models.issue
        if fi.language == "python" and fi.file_path.endswith(".py"):
            module_path = fi.file_path[:-3].replace("/", ".")
            # Remove __init__ suffix for package modules
            if module_path.endswith(".__init__"):
                module_path = module_path[:-9]
            lookup[module_path] = file_node_id

            # Also register short form (last component)
            parts = module_path.split(".")
            if len(parts) > 1:
                lookup[parts[-1]] = file_node_id

    return lookup


def _build_module_lookup_from_nodes(
    nodes: list[GraphNode],
) -> dict[str, int]:
    """Build module lookup from existing GraphNode objects (for incremental updates)."""
    lookup: dict[str, int] = {}

    for node in nodes:
        if node.node_type != "file":
            continue

        file_node_id = node.id

        if node.language == "python" and node.file_path.endswith(".py"):
            module_path = node.file_path[:-3].replace("/", ".")
            if module_path.endswith(".__init__"):
                module_path = module_path[:-9]
            lookup[module_path] = file_node_id

            parts = module_path.split(".")
            if len(parts) > 1:
                lookup[parts[-1]] = file_node_id

    return lookup


# ── File Mapping Migration ────────────────────────────────────────


async def _migrate_file_mappings(
    session,
    repo_id: int,
    file_path_to_node: dict[str, int],
) -> int:
    """Convert existing file_mappings to graph edges."""
    result = await session.execute(
        select(FileMapping).where(FileMapping.repo_id == repo_id)
    )
    mappings = result.scalars().all()

    edge_count = 0
    for mapping in mappings:
        source_node_id = file_path_to_node.get(mapping.file_path)
        if not source_node_id:
            continue

        if mapping.milestone_id:
            session.add(GraphEdge(
                repo_id=repo_id,
                source_node_id=source_node_id,
                target_node_id=None,
                edge_type="belongs_to_milestone",
                target_entity_type="milestone",
                target_entity_id=mapping.milestone_id,
                confidence=mapping.confidence,
            ))
            edge_count += 1

        if mapping.issue_id:
            session.add(GraphEdge(
                repo_id=repo_id,
                source_node_id=source_node_id,
                target_node_id=None,
                edge_type="belongs_to_issue",
                target_entity_type="issue",
                target_entity_id=mapping.issue_id,
                confidence=mapping.confidence,
            ))
            edge_count += 1

    return edge_count
