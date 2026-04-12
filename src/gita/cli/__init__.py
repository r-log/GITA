"""``gita`` command-line entry point.

Subcommands:
    gita index <path> [--name NAME]
    gita repos
    gita stats <repo>
    gita query symbol       <repo> <query>
    gita query neighborhood <repo> <file>
    gita query history      <repo> <file>
    gita query load-bearing <repo>
    gita onboard <repo> [--post-to | --create-issues]

Split into three modules:
- ``cli.commands``   — async command handlers + onboard flows
- ``cli.formatters`` — pure plain-text formatters
- ``cli/__init__``   — argparse wiring + ``main()``
"""
from __future__ import annotations

import argparse
import asyncio
import sys

# Windows: switch stdout/stderr to UTF-8 so we can print source code that
# contains non-cp1252 characters (emoji, CJK, etc.) without crashing.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

from gita import __version__  # noqa: E402
from gita.cli.commands import (  # noqa: E402
    _DEFAULT_MAX_ISSUES,
    cmd_index,
    cmd_onboard,
    cmd_query_history,
    cmd_query_load_bearing,
    cmd_query_neighborhood,
    cmd_query_symbol,
    cmd_repos,
    cmd_stats,
)


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
            "Wrap the onboarding output as a single GitHub comment Decision "
            "and route through execute_decision. Behavior depends on "
            "WRITE_MODE: shadow = log only (default), comment = post a comment."
        ),
    )
    onboard_p.add_argument(
        "--create-issues",
        default=None,
        metavar="OWNER/REPO",
        help=(
            "Wrap each milestone as a create_issue Decision against the "
            "target repo and route each through execute_decision. "
            "Mutually exclusive with --post-to. In WRITE_MODE=comment, "
            "--fallback-issue is required so downgrades have a landing "
            "place."
        ),
    )
    onboard_p.add_argument(
        "--fallback-issue",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Issue number on the target repo where --create-issues "
            "downgrade comments land when WRITE_MODE=comment."
        ),
    )
    onboard_p.add_argument(
        "--max-issues",
        type=int,
        default=_DEFAULT_MAX_ISSUES,
        metavar="N",
        help=(
            f"Maximum issues a single --create-issues invocation can create "
            f"(default {_DEFAULT_MAX_ISSUES}). Safety cap to prevent misfires "
            f"from flooding the target repo."
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
