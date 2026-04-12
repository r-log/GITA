"""``gita`` command-line entry point.

Subcommands:
    gita index <path> [--name NAME]
    gita repos
    gita stats <repo>
    gita query symbol       <repo> <query>
    gita query neighborhood <repo> <file>
    gita query history      <repo> <file>

The CLI is a thin wrapper around ``index_repository`` + the three view
functions. It opens its own async session, prints plain text, and never
swallows exceptions — errors bubble up with a nonzero exit code.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Windows: switch stdout/stderr to UTF-8 so we can print source code that
# contains non-cp1252 characters (emoji, CJK, etc.) without crashing.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import func, select  # noqa: E402

from gita import __version__  # noqa: E402
from gita.agents.decisions import (  # noqa: E402
    Outcome,
    WriteMode,
    execute_decision,
)
from gita.agents.onboarding import (  # noqa: E402
    OnboardingError,
    build_onboarding_comment_decision,
    run_onboarding,
)
from gita.agents.types import OnboardingResult  # noqa: E402
from gita.config import settings  # noqa: E402
from gita.db.models import CodeIndex, ImportEdge, Repo  # noqa: E402
from gita.db.session import SessionLocal  # noqa: E402
from gita.github.auth import GithubAppAuth  # noqa: E402
from gita.github.client import GithubClient  # noqa: E402
from gita.indexer.ingest import IngestResult, index_repository  # noqa: E402
from gita.llm.client import OpenRouterClient  # noqa: E402
from gita.views._common import RepoNotFoundError  # noqa: E402
from gita.views.history import HistoryResult, history_view  # noqa: E402
from gita.views.load_bearing import (  # noqa: E402
    LoadBearingResult,
    load_bearing_view,
)
from gita.views.neighborhood import (  # noqa: E402
    FileNotFoundError,
    NeighborhoodResult,
    neighborhood_view,
)
from gita.views.symbol import SymbolResult, symbol_view  # noqa: E402


# ---------------------------------------------------------------------------
# Printers
# ---------------------------------------------------------------------------
def _fmt_ingest(name: str, root: Path, elapsed: float, result: IngestResult) -> str:
    resolved_pct = (
        (result.edges_resolved / result.edges_total * 100)
        if result.edges_total
        else 0.0
    )
    return (
        f"Indexed {name!r}\n"
        f"  root:      {root}\n"
        f"  files:     {result.files_indexed}\n"
        f"  functions: {result.functions_extracted}\n"
        f"  classes:   {result.classes_extracted}\n"
        f"  imports:   {result.edges_total} "
        f"({result.edges_resolved} resolved, "
        f"{result.edges_total - result.edges_resolved} unresolved, "
        f"{resolved_pct:.0f}% rate)\n"
        f"  head:      {result.head_sha or '<not a git repo>'}\n"
        f"  elapsed:   {elapsed:.2f}s"
    )


def _fmt_repos(rows: list[tuple[Repo, int, int]]) -> str:
    if not rows:
        return "(no repos indexed)"
    lines = [f"{'NAME':<30} {'FILES':>6} {'INDEXED':<20}  ROOT"]
    for repo, file_count, _ in rows:
        indexed = (
            repo.indexed_at.strftime("%Y-%m-%d %H:%M")
            if repo.indexed_at
            else "-"
        )
        lines.append(
            f"{repo.name:<30} {file_count:>6} {indexed:<20}  {repo.root_path}"
        )
    return "\n".join(lines)


def _fmt_stats(
    repo: Repo,
    file_count: int,
    by_language: dict[str, int],
    total_functions: int,
    total_classes: int,
    total_interfaces: int,
    edges_total: int,
    edges_resolved: int,
) -> str:
    resolved_pct = (edges_resolved / edges_total * 100) if edges_total else 0.0
    head = repo.head_sha[:7] if repo.head_sha else "-"
    indexed = (
        repo.indexed_at.strftime("%Y-%m-%d %H:%M") if repo.indexed_at else "-"
    )

    lines = [
        f"Repo: {repo.name}",
        f"  root:    {repo.root_path}",
        f"  head:    {head}",
        f"  indexed: {indexed}",
        "",
        f"Files: {file_count}",
    ]
    for lang in sorted(by_language):
        lines.append(f"  {lang:<12} {by_language[lang]}")
    lines.extend(
        [
            "",
            "Symbols:",
            f"  functions:  {total_functions}",
            f"  classes:    {total_classes}",
        ]
    )
    if total_interfaces:
        lines.append(f"  interfaces: {total_interfaces}")
    lines.extend(
        [
            "",
            f"Imports: {edges_total} total, "
            f"{edges_resolved} resolved ({resolved_pct:.0f}%), "
            f"{edges_total - edges_resolved} unresolved",
        ]
    )
    return "\n".join(lines)


def _fmt_symbol_result(result: SymbolResult) -> str:
    if result.total_matches == 0:
        return f"No symbol matches {result.query!r}"

    header = (
        f"{result.total_matches} match{'es' if result.total_matches != 1 else ''} "
        f"for {result.query!r}"
    )
    if result.truncated:
        header += f" (showing first {len(result.matches)})"

    chunks = [header, ""]
    for i, match in enumerate(result.matches, start=1):
        label = (
            f"{match.parent_class}.{match.name}"
            if match.parent_class
            else match.name
        )
        chunks.append(
            f"[{i}] {match.file_path}:{match.start_line}-{match.end_line}  "
            f"({match.kind} {label})"
        )
        chunks.append(match.code)
        chunks.append("")
    return "\n".join(chunks).rstrip()


def _fmt_neighborhood_result(result: NeighborhoodResult) -> str:
    file = result.file
    lines = [
        f"File: {file.file_path}  ({file.language}, {file.line_count} lines)",
    ]

    if file.symbol_summary:
        lines.append("  symbols:")
        for brief in file.symbol_summary:
            parent = f" in {brief.parent_class}" if brief.parent_class else ""
            lines.append(
                f"    line {brief.line:>4}  {brief.kind:<16} {brief.name}{parent}"
            )

    lines.append("")
    if result.imports:
        lines.append(f"imports ({len(result.imports)}):")
        for f in result.imports:
            lines.append(f"  -> {f.file_path}")
    else:
        lines.append("imports: (none resolved)")

    if result.unresolved_imports:
        lines.append(f"unresolved imports ({len(result.unresolved_imports)}):")
        for raw in result.unresolved_imports[:10]:
            lines.append(f"  ?  {raw}")
        if len(result.unresolved_imports) > 10:
            lines.append(
                f"  ... and {len(result.unresolved_imports) - 10} more"
            )

    lines.append("")
    if result.imported_by:
        lines.append(f"imported by ({len(result.imported_by)}):")
        for f in result.imported_by:
            lines.append(f"  <- {f.file_path}")
    else:
        lines.append("imported by: (nothing)")

    lines.append("")
    if result.siblings:
        lines.append(f"siblings ({len(result.siblings)}):")
        for f in result.siblings:
            lines.append(f"  |  {f.file_path}")
    return "\n".join(lines)


def _fmt_load_bearing_result(result: LoadBearingResult) -> str:
    if not result.files:
        return f"No files indexed for {result.repo_name!r}"

    header = (
        f"Load-bearing files for {result.repo_name} "
        f"(top {len(result.files)} of {result.total_files}):"
    )
    lines = [header, ""]
    for rank, ranked in enumerate(result.files, start=1):
        lines.append(
            f"  {rank:>2}. [in:{ranked.in_degree:>3}]  {ranked.file_path}  "
            f"({ranked.language}, {ranked.line_count} lines)"
        )
        shown = ranked.symbol_summary[:6]
        for brief in shown:
            parent = (
                f" in {brief.parent_class}" if brief.parent_class else ""
            )
            lines.append(
                f"          line {brief.line:>4}  {brief.kind:<16} "
                f"{brief.name}{parent}"
            )
        if len(ranked.symbol_summary) > len(shown):
            lines.append(
                f"          ... and {len(ranked.symbol_summary) - len(shown)} more"
            )
        lines.append("")
    return "\n".join(lines).rstrip()


def _fmt_onboarding_result(result: OnboardingResult) -> str:
    lines = [
        f"Onboarding: {result.repo_name}",
        "",
        "project_summary:",
        f"  {result.project_summary}",
        "",
    ]

    if result.findings:
        lines.append(f"findings ({len(result.findings)}):")
        for i, finding in enumerate(result.findings):
            lines.append(
                f"  [{i}] {finding.severity:<8} {finding.kind:<10} "
                f"{finding.file}:{finding.line}"
            )
            lines.append(f"      {finding.description}")
            if finding.fix_sketch:
                lines.append(f"      fix: {finding.fix_sketch}")
    else:
        lines.append("findings: (none)")

    lines.append("")
    if result.milestones:
        lines.append(f"milestones ({len(result.milestones)}):")
        for i, milestone in enumerate(result.milestones):
            lines.append(
                f"  [{i}] {milestone.title}  (confidence {milestone.confidence:.2f})"
            )
            lines.append(f"      {milestone.summary}")
            lines.append(
                f"      findings: {', '.join(str(j) for j in milestone.finding_indices)}"
            )
    else:
        lines.append("milestones: (none)")

    lines.append("")
    lines.append(f"overall confidence: {result.confidence:.2f}")
    return "\n".join(lines)


def _fmt_history_result(result: HistoryResult) -> str:
    lines = [f"File: {result.file_path}"]

    if not result.git_available:
        lines.append("  (git not available / repo root missing)")
        return "\n".join(lines)

    if result.recent_commits:
        lines.append(f"recent commits ({len(result.recent_commits)}):")
        for c in result.recent_commits:
            date = c.date.split("T")[0] if "T" in c.date else c.date
            lines.append(
                f"  {c.short_sha}  {date}  {c.author:<16}  {c.message}"
            )
    else:
        lines.append("recent commits: (none)")

    if result.blame_summary:
        lines.append("")
        lines.append("blame:")
        for author, count in sorted(
            result.blame_summary.items(), key=lambda kv: -kv[1]
        ):
            lines.append(f"  {author:<24} {count} lines")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
async def cmd_index(args: argparse.Namespace) -> int:
    import time

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    name = args.name or root.name

    async with SessionLocal() as session:
        t0 = time.time()
        result = await index_repository(session, name, root)
        await session.commit()
        elapsed = time.time() - t0
    print(_fmt_ingest(name, root, elapsed, result))
    return 0


async def cmd_repos(args: argparse.Namespace) -> int:  # noqa: ARG001
    async with SessionLocal() as session:
        stmt = select(
            Repo,
            func.count(CodeIndex.id).label("files"),
            func.coalesce(func.sum(CodeIndex.line_count), 0).label("lines"),
        ).outerjoin(CodeIndex, CodeIndex.repo_id == Repo.id).group_by(Repo.id)
        rows = (await session.execute(stmt)).all()
    print(_fmt_repos(list(rows)))
    return 0


async def cmd_stats(args: argparse.Namespace) -> int:
    async with SessionLocal() as session:
        repo_stmt = select(Repo).where(Repo.name == args.repo)
        repo = (await session.execute(repo_stmt)).scalar_one_or_none()
        if repo is None:
            print(f"error: no such repo: {args.repo!r}", file=sys.stderr)
            return 1

        files_stmt = select(CodeIndex).where(CodeIndex.repo_id == repo.id)
        files = (await session.execute(files_stmt)).scalars().all()

        by_language: dict[str, int] = {}
        total_functions = 0
        total_classes = 0
        total_interfaces = 0
        for row in files:
            by_language[row.language] = by_language.get(row.language, 0) + 1
            structure = row.structure or {}
            total_functions += len(structure.get("functions", []))
            for cls in structure.get("classes", []):
                if cls.get("kind") == "interface":
                    total_interfaces += 1
                else:
                    total_classes += 1

        edges_total_stmt = select(func.count(ImportEdge.id)).where(
            ImportEdge.repo_id == repo.id
        )
        edges_resolved_stmt = (
            select(func.count(ImportEdge.id))
            .where(ImportEdge.repo_id == repo.id)
            .where(ImportEdge.dst_file.isnot(None))
        )
        edges_total = (await session.execute(edges_total_stmt)).scalar_one()
        edges_resolved = (
            await session.execute(edges_resolved_stmt)
        ).scalar_one()

    print(
        _fmt_stats(
            repo,
            len(files),
            by_language,
            total_functions,
            total_classes,
            total_interfaces,
            edges_total,
            edges_resolved,
        )
    )
    return 0


async def cmd_query_symbol(args: argparse.Namespace) -> int:
    async with SessionLocal() as session:
        try:
            result = await symbol_view(session, args.repo, args.query)
        except RepoNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(_fmt_symbol_result(result))
    return 0


async def cmd_query_neighborhood(args: argparse.Namespace) -> int:
    async with SessionLocal() as session:
        try:
            result = await neighborhood_view(session, args.repo, args.file_path)
        except RepoNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(_fmt_neighborhood_result(result))
    return 0


async def cmd_query_load_bearing(args: argparse.Namespace) -> int:
    async with SessionLocal() as session:
        try:
            result = await load_bearing_view(
                session, args.repo, limit=args.limit
            )
        except RepoNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(_fmt_load_bearing_result(result))
    return 0


def _parse_post_to(value: str) -> tuple[str, int]:
    """Parse ``owner/repo#123`` into ``("owner/repo", 123)``."""
    if "#" not in value:
        raise ValueError(
            f"--post-to expects owner/repo#issue_number, got {value!r}"
        )
    repo_full, issue_str = value.rsplit("#", 1)
    if repo_full.count("/") != 1:
        raise ValueError(
            f"--post-to repo must be owner/repo, got {repo_full!r}"
        )
    try:
        issue_number = int(issue_str)
    except ValueError as exc:
        raise ValueError(
            f"--post-to issue number must be an integer, got {issue_str!r}"
        ) from exc
    return repo_full, issue_number


async def cmd_onboard(args: argparse.Namespace) -> int:
    if not settings.openrouter_api_key:
        print(
            "error: OPENROUTER_API_KEY is not set in .env — cannot call the "
            "onboarding LLM",
            file=sys.stderr,
        )
        return 2

    post_target: tuple[str, int] | None = None
    if args.post_to:
        try:
            post_target = _parse_post_to(args.post_to)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    model = args.model or settings.ai_default_model
    async with OpenRouterClient(
        api_key=settings.openrouter_api_key, default_model=model
    ) as llm:
        async with SessionLocal() as session:
            try:
                result = await run_onboarding(
                    session,
                    args.repo,
                    llm=llm,
                    load_bearing_limit=args.load_bearing,
                    deep_read_limit=args.deep_read,
                )
            except RepoNotFoundError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            except OnboardingError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1

    print(_fmt_onboarding_result(result))

    if post_target is None:
        return 0

    # --- Posting flow: wrap the result in a Decision and route through
    # execute_decision with the current WRITE_MODE from settings. ---
    repo_full, issue_number = post_target
    mode = WriteMode(settings.write_mode)
    decision = build_onboarding_comment_decision(
        result, repo_full_name=repo_full, issue_number=issue_number
    )

    print()
    print(f"--- Posting flow (WRITE_MODE={mode.value}) ---")
    print(f"target: {repo_full}#{issue_number}")
    print(f"decision confidence: {decision.confidence:.2f}")
    print(f"evidence:")
    for bullet in decision.evidence:
        print(f"  - {bullet}")

    if mode == WriteMode.SHADOW:
        # Shadow mode: route through the gate with no client attached.
        # This logs the decision and returns without any network I/O.
        decision_result = await execute_decision(decision, mode=mode)
        print()
        print(f"outcome: {decision_result.outcome.value}")
        print("(shadow mode — no comment was posted; flip WRITE_MODE=comment to post for real)")
        return 0

    # comment / full mode — instantiate the real GitHub client.
    if (
        not settings.github_app_id
        or not settings.github_app_private_key_path
    ):
        print(
            "error: GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH must be set "
            "in .env when WRITE_MODE is not shadow",
            file=sys.stderr,
        )
        return 2

    auth = GithubAppAuth.from_files(
        app_id=settings.github_app_id,
        private_key_path=settings.github_app_private_key_path,
    )
    async with GithubClient(auth=auth) as gh:
        decision_result = await execute_decision(
            decision, mode=mode, client=gh
        )

    print()
    print(f"outcome: {decision_result.outcome.value}")
    if decision_result.executed:
        side = decision_result.side_effect or {}
        url = side.get("html_url") or "(url not returned)"
        print(f"posted: {url}")
    elif decision_result.error:
        print(f"error: {decision_result.error}", file=sys.stderr)
        return 1
    return 0


async def cmd_query_history(args: argparse.Namespace) -> int:
    async with SessionLocal() as session:
        try:
            result = await history_view(session, args.repo, args.file_path)
        except RepoNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(_fmt_history_result(result))
    return 0


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gita",
        description="GitHub Assistant v2 — local repo indexer and query layer",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"gita {__version__}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    index_p = sub.add_parser("index", help="Index a local repo")
    index_p.add_argument("path", help="Path to the repo root on disk")
    index_p.add_argument(
        "--name",
        help="Override the repo name (defaults to the root directory name)",
    )

    sub.add_parser("repos", help="List indexed repos")

    stats_p = sub.add_parser("stats", help="Show stats for one indexed repo")
    stats_p.add_argument("repo")

    onboard_p = sub.add_parser(
        "onboard",
        help="Run the onboarding agent against an indexed repo (uses OpenRouter)",
    )
    onboard_p.add_argument("repo")
    onboard_p.add_argument(
        "--model",
        default=None,
        help="Override the LLM model (defaults to AI_DEFAULT_MODEL)",
    )
    onboard_p.add_argument(
        "--load-bearing",
        type=int,
        default=10,
        help="How many load-bearing files to show the picker (default 10)",
    )
    onboard_p.add_argument(
        "--deep-read",
        type=int,
        default=5,
        help="How many files the LLM is allowed to read deeply (default 5)",
    )
    onboard_p.add_argument(
        "--post-to",
        default=None,
        metavar="OWNER/REPO#ISSUE",
        help=(
            "Wrap the onboarding output as a GitHub comment Decision and "
            "route through execute_decision. Behavior depends on WRITE_MODE: "
            "shadow = log only (default), comment = post a comment."
        ),
    )

    query_p = sub.add_parser("query", help="Query the index")
    query_sub = query_p.add_subparsers(dest="query_type", required=True)

    q_sym = query_sub.add_parser("symbol", help="Find a symbol by name")
    q_sym.add_argument("repo")
    q_sym.add_argument(
        "query", help="Symbol name (use 'ClassName.method' to scope)"
    )

    q_nbh = query_sub.add_parser(
        "neighborhood", help="Show imports/importers/siblings for a file"
    )
    q_nbh.add_argument("repo")
    q_nbh.add_argument("file_path")

    q_load = query_sub.add_parser(
        "load-bearing",
        help="Rank files by in-degree in the import graph",
    )
    q_load.add_argument("repo")
    q_load.add_argument(
        "--limit",
        type=int,
        default=10,
        help="How many files to return (default 10, max 100)",
    )

    q_hist = query_sub.add_parser(
        "history", help="Show git log + blame for a file"
    )
    q_hist.add_argument("repo")
    q_hist.add_argument("file_path")

    return parser


_HANDLERS = {
    "index": cmd_index,
    "repos": cmd_repos,
    "stats": cmd_stats,
    "onboard": cmd_onboard,
    ("query", "symbol"): cmd_query_symbol,
    ("query", "neighborhood"): cmd_query_neighborhood,
    ("query", "load-bearing"): cmd_query_load_bearing,
    ("query", "history"): cmd_query_history,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "query":
        handler = _HANDLERS.get((args.command, args.query_type))
    else:
        handler = _HANDLERS.get(args.command)

    if handler is None:
        parser.print_help()
        return 2

    return asyncio.run(handler(args))


if __name__ == "__main__":
    sys.exit(main())
