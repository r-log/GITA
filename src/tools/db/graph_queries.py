"""
Graph query tools for agents — targeted graph traversal replacing crude substring search.

Each tool answers a specific question that agents need:
- Blast radius of a set of changes
- File dependencies and dependents
- File ownership (which issues/milestones)
- Symbol usages across the codebase
- Milestone file coverage
- Focused code map (subgraph context)
"""

from __future__ import annotations

import structlog
from sqlalchemy import select, text

log = structlog.get_logger()

from src.core.database import async_session
from src.models.graph_node import GraphNode
from src.models.graph_edge import GraphEdge
from src.models.code_index import CodeIndex
from src.models.pr_file_change import PrFileChange
from src.indexer.code_map import generate_code_map
from src.tools.base import Tool, ToolResult


# ── get_file_dependents ──────────────────────────────────────────


async def _get_file_dependents(repo_id: int, file_path: str) -> ToolResult:
    """Find all files that import/depend on the given file."""
    try:
        async with async_session() as session:
            # Find the file node
            target_node = await session.execute(
                select(GraphNode.id).where(
                    GraphNode.repo_id == repo_id,
                    GraphNode.file_path == file_path,
                    GraphNode.node_type == "file",
                )
            )
            target_id = target_node.scalar_one_or_none()
            if not target_id:
                return ToolResult(success=True, data={"dependents": [], "count": 0})

            # Find all files that import this file
            result = await session.execute(
                select(GraphNode.file_path, GraphNode.language).join(
                    GraphEdge, GraphEdge.source_node_id == GraphNode.id
                ).where(
                    GraphEdge.target_node_id == target_id,
                    GraphEdge.edge_type == "imports",
                    GraphNode.node_type == "file",
                )
            )
            dependents = [{"file_path": r[0], "language": r[1]} for r in result.all()]

        return ToolResult(success=True, data={"dependents": dependents, "count": len(dependents)})
    except Exception as e:
        log.warning("graph_query_failed", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


def make_get_file_dependents(repo_id: int) -> Tool:
    return Tool(
        name="get_file_dependents",
        description="Find all files that import or depend on the given file. "
                    "Use this to understand what breaks if a file changes.",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The file path to find dependents for (e.g. 'src/models/user.py')",
                },
            },
            "required": ["file_path"],
        },
        handler=lambda file_path: _get_file_dependents(repo_id, file_path),
    )


# ── get_file_dependencies ────────────────────────────────────────


async def _get_file_dependencies(repo_id: int, file_path: str) -> ToolResult:
    """Find all files that the given file imports/depends on."""
    try:
        async with async_session() as session:
            source_node = await session.execute(
                select(GraphNode.id).where(
                    GraphNode.repo_id == repo_id,
                    GraphNode.file_path == file_path,
                    GraphNode.node_type == "file",
                )
            )
            source_id = source_node.scalar_one_or_none()
            if not source_id:
                return ToolResult(success=True, data={"dependencies": [], "count": 0})

            result = await session.execute(
                select(GraphNode.file_path, GraphNode.language).join(
                    GraphEdge, GraphEdge.target_node_id == GraphNode.id
                ).where(
                    GraphEdge.source_node_id == source_id,
                    GraphEdge.edge_type == "imports",
                    GraphNode.node_type == "file",
                )
            )
            deps = [{"file_path": r[0], "language": r[1]} for r in result.all()]

        return ToolResult(success=True, data={"dependencies": deps, "count": len(deps)})
    except Exception as e:
        log.warning("graph_query_failed", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


def make_get_file_dependencies(repo_id: int) -> Tool:
    return Tool(
        name="get_file_dependencies",
        description="Find all files that the given file imports or depends on.",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The file path to find dependencies for",
                },
            },
            "required": ["file_path"],
        },
        handler=lambda file_path: _get_file_dependencies(repo_id, file_path),
    )


# ── get_blast_radius ─────────────────────────────────────────────


async def _get_blast_radius(
    repo_id: int,
    file_paths: list[str],
    depth: int = 2,
) -> ToolResult:
    """
    Given changed files, find the transitive set of affected files, issues, and milestones.

    Uses a recursive CTE to walk import edges up to `depth` hops.
    Then joins affected files to entity edges to find impacted project entities.
    """
    if not file_paths:
        return ToolResult(success=True, data={
            "affected_files": [], "affected_issues": [], "affected_milestones": [],
            "direct_files": file_paths, "depth": depth,
        })

    try:
        async with async_session() as session:
            # Recursive CTE for transitive dependents
            cte_query = text("""
                WITH RECURSIVE affected AS (
                    SELECT gn.id, gn.file_path, 0 AS depth
                    FROM graph_nodes gn
                    WHERE gn.repo_id = :repo_id
                      AND gn.file_path = ANY(:file_paths)
                      AND gn.node_type = 'file'

                    UNION

                    SELECT gn2.id, gn2.file_path, a.depth + 1
                    FROM affected a
                    JOIN graph_edges ge ON ge.target_node_id = a.id AND ge.edge_type = 'imports'
                    JOIN graph_nodes gn2 ON gn2.id = ge.source_node_id
                    WHERE a.depth < :max_depth
                      AND gn2.repo_id = :repo_id
                )
                SELECT DISTINCT file_path, MIN(depth) AS min_depth
                FROM affected
                GROUP BY file_path
                ORDER BY min_depth, file_path
            """)

            result = await session.execute(
                cte_query, {"repo_id": repo_id, "file_paths": file_paths, "max_depth": depth}
            )
            affected_files = [
                {"file_path": row[0], "depth": row[1]}
                for row in result.all()
            ]

            # Find affected issues and milestones via entity edges
            affected_file_paths = [f["file_path"] for f in affected_files]

            affected_issues = []
            affected_milestones = []

            if affected_file_paths:
                # Get file node IDs for affected files
                node_result = await session.execute(
                    select(GraphNode.id, GraphNode.file_path).where(
                        GraphNode.repo_id == repo_id,
                        GraphNode.file_path.in_(affected_file_paths),
                        GraphNode.node_type == "file",
                    )
                )
                file_nodes = {row[0]: row[1] for row in node_result.all()}
                node_ids = list(file_nodes.keys())

                if node_ids:
                    # Issues
                    issue_result = await session.execute(
                        select(
                            GraphEdge.target_entity_id,
                            GraphEdge.confidence,
                            GraphNode.file_path,
                        ).join(
                            GraphNode, GraphNode.id == GraphEdge.source_node_id
                        ).where(
                            GraphEdge.repo_id == repo_id,
                            GraphEdge.source_node_id.in_(node_ids),
                            GraphEdge.edge_type == "belongs_to_issue",
                        )
                    )
                    issue_map: dict[int, dict] = {}
                    for entity_id, confidence, fpath in issue_result.all():
                        if entity_id not in issue_map:
                            issue_map[entity_id] = {
                                "issue_id": entity_id,
                                "confidence": confidence,
                                "via_files": [],
                            }
                        issue_map[entity_id]["via_files"].append(fpath)
                    affected_issues = list(issue_map.values())

                    # Milestones
                    ms_result = await session.execute(
                        select(
                            GraphEdge.target_entity_id,
                            GraphEdge.confidence,
                            GraphNode.file_path,
                        ).join(
                            GraphNode, GraphNode.id == GraphEdge.source_node_id
                        ).where(
                            GraphEdge.repo_id == repo_id,
                            GraphEdge.source_node_id.in_(node_ids),
                            GraphEdge.edge_type == "belongs_to_milestone",
                        )
                    )
                    ms_map: dict[int, dict] = {}
                    for entity_id, confidence, fpath in ms_result.all():
                        if entity_id not in ms_map:
                            ms_map[entity_id] = {
                                "milestone_id": entity_id,
                                "confidence": confidence,
                                "via_files": [],
                            }
                        ms_map[entity_id]["via_files"].append(fpath)
                    affected_milestones = list(ms_map.values())

        return ToolResult(success=True, data={
            "affected_files": affected_files,
            "affected_issues": affected_issues,
            "affected_milestones": affected_milestones,
            "direct_files": file_paths,
            "depth": depth,
            "total_affected": len(affected_files),
        })
    except Exception as e:
        log.warning("graph_query_failed", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


def make_get_blast_radius(repo_id: int) -> Tool:
    return Tool(
        name="get_blast_radius",
        description="Given a list of changed file paths, find the transitive set of affected files, "
                    "issues, and milestones. Walks the import graph up to N hops to determine impact. "
                    "Use this after fetching PR files to understand the full impact of changes.",
        parameters={
            "type": "object",
            "properties": {
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of changed file paths to analyze",
                },
                "depth": {
                    "type": "integer",
                    "description": "Maximum hop depth for transitive dependency walk (default: 2)",
                    "default": 2,
                },
            },
            "required": ["file_paths"],
        },
        handler=lambda file_paths, depth=2: _get_blast_radius(repo_id, file_paths, depth),
    )


# ── get_file_ownership ───────────────────────────────────────────


async def _get_file_ownership(repo_id: int, file_paths: list[str]) -> ToolResult:
    """Find which issues and milestones own the given files."""
    try:
        async with async_session() as session:
            # Get file node IDs
            node_result = await session.execute(
                select(GraphNode.id, GraphNode.file_path).where(
                    GraphNode.repo_id == repo_id,
                    GraphNode.file_path.in_(file_paths),
                    GraphNode.node_type == "file",
                )
            )
            file_nodes = {row[0]: row[1] for row in node_result.all()}

            if not file_nodes:
                return ToolResult(success=True, data={"files": []})

            # Query entity edges for these file nodes
            edge_result = await session.execute(
                select(
                    GraphNode.file_path,
                    GraphEdge.edge_type,
                    GraphEdge.target_entity_type,
                    GraphEdge.target_entity_id,
                    GraphEdge.confidence,
                ).join(
                    GraphNode, GraphNode.id == GraphEdge.source_node_id
                ).where(
                    GraphEdge.repo_id == repo_id,
                    GraphEdge.source_node_id.in_(list(file_nodes.keys())),
                    GraphEdge.edge_type.in_(["belongs_to_issue", "belongs_to_milestone"]),
                )
            )

            ownership: dict[str, dict] = {fp: {"file_path": fp, "issues": [], "milestones": []} for fp in file_paths}
            for fpath, edge_type, _entity_type, entity_id, confidence in edge_result.all():
                if edge_type == "belongs_to_issue":
                    ownership[fpath]["issues"].append({"id": entity_id, "confidence": confidence})
                elif edge_type == "belongs_to_milestone":
                    ownership[fpath]["milestones"].append({"id": entity_id, "confidence": confidence})

        return ToolResult(success=True, data={"files": list(ownership.values())})
    except Exception as e:
        log.warning("graph_query_failed", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


def make_get_file_ownership(repo_id: int) -> Tool:
    return Tool(
        name="get_file_ownership",
        description="Find which issues and milestones own the given files. "
                    "Use this to check milestone alignment of changed files.",
        parameters={
            "type": "object",
            "properties": {
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of file paths to check ownership for",
                },
            },
            "required": ["file_paths"],
        },
        handler=lambda file_paths: _get_file_ownership(repo_id, file_paths),
    )


# ── get_symbol_usages ────────────────────────────────────────────


async def _get_symbol_usages(repo_id: int, symbol_name: str) -> ToolResult:
    """Find where a class/function/symbol is used across the codebase."""
    try:
        async with async_session() as session:
            # Find the symbol node(s) matching the name
            symbol_result = await session.execute(
                select(GraphNode).where(
                    GraphNode.repo_id == repo_id,
                    GraphNode.name == symbol_name,
                    GraphNode.node_type.in_(["class", "function", "method"]),
                )
            )
            symbols = symbol_result.scalars().all()

            if not symbols:
                return ToolResult(success=True, data={"usages": [], "symbol": symbol_name})

            symbol_ids = [s.id for s in symbols]
            symbol_info = [
                {"qualified_name": s.qualified_name, "file_path": s.file_path, "type": s.node_type}
                for s in symbols
            ]

            # Find files that import files containing these symbols
            symbol_file_paths = list({s.file_path for s in symbols})
            file_node_result = await session.execute(
                select(GraphNode.id).where(
                    GraphNode.repo_id == repo_id,
                    GraphNode.file_path.in_(symbol_file_paths),
                    GraphNode.node_type == "file",
                )
            )
            file_node_ids = [row[0] for row in file_node_result.all()]

            usages = []
            if file_node_ids:
                # Files that import the symbol's file
                import_result = await session.execute(
                    select(GraphNode.file_path, GraphNode.language).join(
                        GraphEdge, GraphEdge.source_node_id == GraphNode.id
                    ).where(
                        GraphEdge.target_node_id.in_(file_node_ids),
                        GraphEdge.edge_type == "imports",
                        GraphNode.node_type == "file",
                    )
                )
                usages = [
                    {"file_path": r[0], "language": r[1], "relationship": "imports"}
                    for r in import_result.all()
                ]

            # Also find inheritance usages
            inherits_result = await session.execute(
                select(GraphNode.qualified_name, GraphNode.file_path).join(
                    GraphEdge, GraphEdge.source_node_id == GraphNode.id
                ).where(
                    GraphEdge.target_node_id.in_(symbol_ids),
                    GraphEdge.edge_type == "inherits",
                )
            )
            for qname, fpath in inherits_result.all():
                usages.append({
                    "file_path": fpath,
                    "qualified_name": qname,
                    "relationship": "inherits",
                })

        return ToolResult(success=True, data={
            "symbol": symbol_name,
            "definitions": symbol_info,
            "usages": usages,
            "usage_count": len(usages),
        })
    except Exception as e:
        log.warning("graph_query_failed", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


def make_get_symbol_usages(repo_id: int) -> Tool:
    return Tool(
        name="get_symbol_usages",
        description="Find where a class, function, or symbol is used across the codebase. "
                    "Use this when a symbol was changed to find all callers/inheritors that may be affected.",
        parameters={
            "type": "object",
            "properties": {
                "symbol_name": {
                    "type": "string",
                    "description": "The name of the class/function to search for (e.g. 'UserModel', 'authenticate')",
                },
            },
            "required": ["symbol_name"],
        },
        handler=lambda symbol_name: _get_symbol_usages(repo_id, symbol_name),
    )


# ── get_milestone_file_coverage ──────────────────────────────────


async def _get_milestone_file_coverage(repo_id: int, milestone_id: int) -> ToolResult:
    """
    Find files belonging to a milestone and which have been changed in recent PRs.

    Provides code-level progress metrics beyond just issue counts.
    """
    try:
        async with async_session() as session:
            # Get file nodes that belong to this milestone
            file_result = await session.execute(
                select(GraphNode.file_path, GraphEdge.confidence).join(
                    GraphEdge, GraphEdge.source_node_id == GraphNode.id
                ).where(
                    GraphEdge.repo_id == repo_id,
                    GraphEdge.edge_type == "belongs_to_milestone",
                    GraphEdge.target_entity_type == "milestone",
                    GraphEdge.target_entity_id == milestone_id,
                    GraphNode.node_type == "file",
                )
            )
            milestone_files = {row[0]: row[1] for row in file_result.all()}

            if not milestone_files:
                return ToolResult(success=True, data={
                    "milestone_id": milestone_id,
                    "total_files": 0,
                    "files_with_changes": 0,
                    "unchanged_files": [],
                    "changed_files": [],
                })

            # Check which of these files have been changed in PRs
            change_result = await session.execute(
                select(PrFileChange.file_path).distinct().where(
                    PrFileChange.repo_id == repo_id,
                    PrFileChange.file_path.in_(list(milestone_files.keys())),
                )
            )
            changed_file_paths = {row[0] for row in change_result.all()}

            changed = [fp for fp in milestone_files if fp in changed_file_paths]
            unchanged = [fp for fp in milestone_files if fp not in changed_file_paths]

        return ToolResult(success=True, data={
            "milestone_id": milestone_id,
            "total_files": len(milestone_files),
            "files_with_changes": len(changed),
            "changed_files": changed,
            "unchanged_files": unchanged,
            "completion_estimate": round(len(changed) / len(milestone_files) * 100, 1) if milestone_files else 0,
        })
    except Exception as e:
        log.warning("graph_query_failed", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


def make_get_milestone_file_coverage(repo_id: int) -> Tool:
    return Tool(
        name="get_milestone_file_coverage",
        description="Find files belonging to a milestone and check which have been changed in PRs. "
                    "Provides code-level progress metrics: 'Milestone has 20 files, 12 touched in PRs, 8 untouched.'",
        parameters={
            "type": "object",
            "properties": {
                "milestone_id": {
                    "type": "integer",
                    "description": "The database ID of the milestone to check coverage for",
                },
            },
            "required": ["milestone_id"],
        },
        handler=lambda milestone_id: _get_milestone_file_coverage(repo_id, milestone_id),
    )


# ── get_focused_code_map ─────────────────────────────────────────


async def _get_focused_code_map(
    repo_id: int,
    file_paths: list[str],
    depth: int = 1,
) -> ToolResult:
    """
    Generate a code map of just the given files and their immediate graph neighbors.

    Solves the scalability problem: instead of a 30KB+ full repo code map,
    agents get 2-5KB of highly relevant context centered on the files they care about.
    """
    try:
        async with async_session() as session:
            # Start with the requested files
            target_files = set(file_paths)

            if depth > 0:
                # Get file node IDs
                node_result = await session.execute(
                    select(GraphNode.id, GraphNode.file_path).where(
                        GraphNode.repo_id == repo_id,
                        GraphNode.file_path.in_(file_paths),
                        GraphNode.node_type == "file",
                    )
                )
                file_nodes = {row[0]: row[1] for row in node_result.all()}

                if file_nodes:
                    node_ids = list(file_nodes.keys())

                    # Get neighbors: files that import these files (dependents)
                    dep_result = await session.execute(
                        select(GraphNode.file_path).join(
                            GraphEdge, GraphEdge.source_node_id == GraphNode.id
                        ).where(
                            GraphEdge.target_node_id.in_(node_ids),
                            GraphEdge.edge_type == "imports",
                            GraphNode.node_type == "file",
                        )
                    )
                    for row in dep_result.all():
                        target_files.add(row[0])

                    # Get neighbors: files these files import (dependencies)
                    imp_result = await session.execute(
                        select(GraphNode.file_path).join(
                            GraphEdge, GraphEdge.target_node_id == GraphNode.id
                        ).where(
                            GraphEdge.source_node_id.in_(node_ids),
                            GraphEdge.edge_type == "imports",
                            GraphNode.node_type == "file",
                        )
                    )
                    for row in imp_result.all():
                        target_files.add(row[0])

            # Fetch CodeIndex records for the focused file set
            result = await session.execute(
                select(CodeIndex).where(
                    CodeIndex.repo_id == repo_id,
                    CodeIndex.file_path.in_(list(target_files)),
                )
            )
            records = result.scalars().all()

        if not records:
            return ToolResult(success=True, data="No code index found for the specified files.")

        records_for_map = [
            {
                "file_path": r.file_path,
                "language": r.language,
                "line_count": r.line_count,
                "structure": r.structure,
            }
            for r in records
        ]

        code_map = generate_code_map(records_for_map, project_name="Focused View")
        return ToolResult(success=True, data=code_map)
    except Exception as e:
        log.warning("graph_query_failed", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


def make_get_focused_code_map(repo_id: int) -> Tool:
    return Tool(
        name="get_focused_code_map",
        description="Generate a code map of just the specified files and their immediate neighbors "
                    "(files they import + files that import them). Returns 2-5KB of highly relevant "
                    "context instead of the full 30KB+ project code map. "
                    "Use this instead of get_code_map for targeted analysis.",
        parameters={
            "type": "object",
            "properties": {
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of file paths to center the code map on",
                },
                "depth": {
                    "type": "integer",
                    "description": "Neighbor depth: 0 = only listed files, 1 = include direct imports/importers (default: 1)",
                    "default": 1,
                },
            },
            "required": ["file_paths"],
        },
        handler=lambda file_paths, depth=1: _get_focused_code_map(repo_id, file_paths, depth),
    )
