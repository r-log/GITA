"""GITA over MCP: the repository's memory, served to coding agents.

    gita mcp                 # in a clone GITA tracks
    gita mcp --repo NAME
    claude mcp add gita -- gita mcp

The tools answer from the stored history (``gita history sync``) and the
code index (``gita index``). They are read-only and make no model calls,
so they are fast and free to call.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gita.history import queries as q

INSTRUCTIONS = """\
GITA is this repository's memory: every merged PR, which later fixes repaired \
which earlier PRs (traced line by line through git), and a code index \
(symbols, includes and imports). Use it while you work:

- Before changing a file or directory: lessons(paths) for what went wrong \
there before, and past_bugs(path) for the details.
- Before finishing a change: review_context(diff=...), then check the change \
against every lesson and past bug it returns.
- To see what a change can break: impact(file or symbol).
- history(path) for how a file has changed; scoreboard() for how often merged \
PRs later needed a fix, and where.

Answers come from git and the index, with no model calls. Traced defects are \
evidence, not proof; read the cited lines. No history is not evidence of safety.\
"""

_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)


def _clamp(limit: int, hi: int = 50) -> int:
    return max(1, min(int(limit), hi))


def build_server(repo_name: str, sessions: async_sessionmaker[AsyncSession]) -> MCPServer:
    server = MCPServer(name="gita", instructions=INSTRUCTIONS)

    async def memory(session: AsyncSession) -> q.Memory:
        # A ToolError keeps its message; anything else reaches the agent as a
        # bare "Error executing tool", and "run gita history sync" is the fix.
        try:
            return await q.load(session, repo_name)
        except q.NotSynced as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        description=(
            "Defects fixed in a file or directory (or whose fix or cause mentions the given "
            "words): what was fixed, which PR introduced it, how long it lived, and the exact "
            "lines. Most recent first."
        ),
        annotations=_READ_ONLY,
    )
    async def past_bugs(
        path: str | None = None, text: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        async with sessions() as session:
            mem = await memory(session)
            return q.past_bugs(mem, path=path, text=text, limit=_clamp(limit))

    @server.tool(
        description=(
            "Lessons from this repository's past fixes that apply to the given paths: same "
            "file first, then the same directory, then parents. Each is a fix's title, i.e. "
            "what had to be corrected."
        ),
        annotations=_READ_ONLY,
    )
    async def lessons(paths: list[str], limit: int = 8) -> dict[str, Any]:
        async with sessions() as session:
            mem = await memory(session)
            return q.lessons(mem, paths, limit=_clamp(limit, 20))

    @server.tool(
        description=(
            "Everything the memory knows about the files a change touches, in one call: their "
            "past bugs, lessons, recent PRs and how many files depend on them. Pass a unified "
            "diff (e.g. `git diff`) or a list of paths. Call it before finishing a change."
        ),
        annotations=_READ_ONLY,
    )
    async def review_context(
        diff: str | None = None, paths: list[str] | None = None
    ) -> dict[str, Any]:
        async with sessions() as session:
            mem = await memory(session)
            return await q.review_context(session, mem, diff=diff, paths=paths)

    @server.tool(
        description=(
            "What depends on a file (the files that include or import it, and its symbols), or, "
            "for a symbol name, where it is defined (signature, doc) and which files mention it."
        ),
        annotations=_READ_ONLY,
    )
    async def impact(target: str, limit: int = 20) -> dict[str, Any]:
        async with sessions() as session:
            mem = await memory(session)
            return await q.impact(session, mem, target, limit=_clamp(limit))

    @server.tool(
        description=(
            "How a file or directory has changed: merged PRs that touched it, by kind, recent "
            "ones, authors, and how many traced defects sit in it."
        ),
        annotations=_READ_ONLY,
    )
    async def history(path: str, limit: int = 10) -> dict[str, Any]:
        async with sessions() as session:
            mem = await memory(session)
            return await q.file_history(session, mem, path, limit=_clamp(limit))

    @server.tool(
        description=(
            "How often merged PRs later needed a fix: overall, by kind of PR, by size, by area "
            "and by month, with time to fix. `since` (YYYY-MM-DD) limits the window. "
            "Read the caveats it returns."
        ),
        annotations=_READ_ONLY,
    )
    async def scoreboard(since: str | None = None) -> dict[str, Any]:
        async with sessions() as session:
            return q.scoreboard(await memory(session), since=since)

    return server
