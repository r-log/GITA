"""
GitHub tools for repository operations: tree, file reading, collaborators.
"""

import structlog

from src.core.github_auth import GitHubClient
from src.tools.base import Tool, ToolResult

log = structlog.get_logger()


async def _get_repo_tree(installation_id: int, repo_full_name: str, ref: str = "HEAD") -> ToolResult:
    """Fetch the full file tree using Git Trees API (recursive, efficient)."""
    client = GitHubClient(installation_id)
    try:
        data = await client.get(
            f"/repos/{repo_full_name}/git/trees/{ref}",
            params={"recursive": "1"},
        )
        # Return simplified tree: just paths and types
        tree = [
            {"path": item["path"], "type": item["type"], "size": item.get("size", 0)}
            for item in data.get("tree", [])
        ]
        return ToolResult(success=True, data=tree)
    except Exception as e:
        log.warning("get_repo_tree_failed", operation="get_repo_tree", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


async def _read_file(installation_id: int, repo_full_name: str, path: str, ref: str = "HEAD") -> ToolResult:
    """Read a specific file's content from the repo."""
    client = GitHubClient(installation_id)
    try:
        data = await client.get(
            f"/repos/{repo_full_name}/contents/{path}",
            params={"ref": ref},
        )
        import base64
        content = base64.b64decode(data["content"]).decode("utf-8")
        return ToolResult(success=True, data={"path": path, "content": content, "size": data.get("size", 0)})
    except Exception as e:
        log.warning("read_file_failed", operation="read_file", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


async def _get_collaborators(installation_id: int, repo_full_name: str) -> ToolResult:
    """List repo collaborators."""
    client = GitHubClient(installation_id)
    try:
        data = await client.get(f"/repos/{repo_full_name}/collaborators")
        collaborators = [
            {"login": c["login"], "permissions": c.get("permissions", {})}
            for c in data
        ]
        return ToolResult(success=True, data=collaborators)
    except Exception as e:
        log.warning("get_collaborators_failed", operation="get_collaborators", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


def make_get_repo_tree(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="get_repo_tree",
        description="Fetch the full file tree of the repository. Returns all file paths, types, and sizes.",
        parameters={
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Git ref (branch/tag/sha). Defaults to HEAD."},
            },
            "required": [],
        },
        handler=lambda ref="HEAD": _get_repo_tree(installation_id, repo_full_name, ref),
    )


def make_read_file(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="read_file",
        description="Read a specific file's content from the repository.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to repo root (e.g. 'src/main.py')"},
                "ref": {"type": "string", "description": "Git ref. Defaults to HEAD."},
            },
            "required": ["path"],
        },
        handler=lambda path, ref="HEAD": _read_file(installation_id, repo_full_name, path, ref),
    )


def make_get_collaborators(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="get_collaborators",
        description="List all collaborators on the repository with their permission levels.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda: _get_collaborators(installation_id, repo_full_name),
    )
