"""Command handlers for the ``gita`` CLI.

Each ``cmd_*`` function takes an ``argparse.Namespace`` and returns an
integer exit code. The main entry point in ``cli/__init__.py`` maps
subcommands to handlers via the ``_HANDLERS`` dict.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import func, select

from gita.agents.decisions import (
    Decision,
    DecisionResult,
    Outcome,
    WriteMode,
    execute_decision,
)
from gita.agents.onboarding import (
    OnboardingError,
    build_onboarding_comment_decision,
    build_onboarding_issue_decisions,
    run_onboarding,
)
from gita.agents.pr_reviewer import (
    PRReviewError,
    build_pr_review_decision,
    parse_pr_files,
    run_pr_review,
)
from gita.agents.types import OnboardingResult
from gita.cli.formatters import (
    fmt_concept_result,
    fmt_history_result,
    fmt_ingest,
    fmt_load_bearing_result,
    fmt_neighborhood_result,
    fmt_onboarding_result,
    fmt_pr_review_result,
    fmt_repos,
    fmt_stats,
    fmt_symbol_result,
)
from gita.config import settings
from gita.db.models import CodeIndex, ImportEdge, Repo
from gita.db.session import SessionLocal
from gita.github.auth import GithubAppAuth
from gita.github.client import GithubClient
from gita.indexer.ingest import index_repository
from gita.llm.client import OpenRouterClient
from gita.views._common import RepoNotFoundError
from gita.views.concept import concept_view
from gita.views.history import history_view
from gita.views.load_bearing import load_bearing_view
from gita.views.neighborhood import FileNotFoundError, neighborhood_view
from gita.views.symbol import symbol_view

# Hard cap on how many issues a single invocation can create. Prevents a
# misfire from flooding the target repo with dozens of issues before anyone
# notices. Configurable via --max-issues CLI flag; defaults to this.
_DEFAULT_MAX_ISSUES = 10


# ---------------------------------------------------------------------------
# Index / repos / stats
# ---------------------------------------------------------------------------
async def cmd_index(args: argparse.Namespace) -> int:
    import time

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    name = args.name or root.name
    force_full = getattr(args, "full", False)
    github_full_name = getattr(args, "github", None)

    async with SessionLocal() as session:
        t0 = time.time()
        result = await index_repository(
            session,
            name,
            root,
            force_full=force_full,
            github_full_name=github_full_name,
        )
        await session.commit()
        elapsed = time.time() - t0
    print(fmt_ingest(name, root, elapsed, result))
    return 0


async def cmd_repos(args: argparse.Namespace) -> int:  # noqa: ARG001
    async with SessionLocal() as session:
        stmt = select(
            Repo,
            func.count(CodeIndex.id).label("files"),
            func.coalesce(func.sum(CodeIndex.line_count), 0).label("lines"),
        ).outerjoin(CodeIndex, CodeIndex.repo_id == Repo.id).group_by(Repo.id)
        rows = (await session.execute(stmt)).all()
    print(fmt_repos(list(rows)))
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
        fmt_stats(
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


# ---------------------------------------------------------------------------
# Query subcommands
# ---------------------------------------------------------------------------
async def cmd_query_symbol(args: argparse.Namespace) -> int:
    async with SessionLocal() as session:
        try:
            result = await symbol_view(session, args.repo, args.query)
        except RepoNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(fmt_symbol_result(result))
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
    print(fmt_neighborhood_result(result))
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
    print(fmt_load_bearing_result(result))
    return 0


async def cmd_query_concept(args: argparse.Namespace) -> int:
    query = " ".join(args.query)
    if not query.strip():
        print("error: query cannot be empty", file=sys.stderr)
        return 2
    async with SessionLocal() as session:
        try:
            result = await concept_view(
                session, args.repo, query, limit=args.limit
            )
        except RepoNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(fmt_concept_result(result))
    return 0


async def cmd_query_history(args: argparse.Namespace) -> int:
    async with SessionLocal() as session:
        try:
            result = await history_view(session, args.repo, args.file_path)
        except RepoNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(fmt_history_result(result))
    return 0


# ---------------------------------------------------------------------------
# Review-PR command
# ---------------------------------------------------------------------------
def _parse_pr_target(value: str) -> tuple[str, str, int]:
    """Parse ``OWNER/REPO#N`` into ``(owner, repo, pr_number)``."""
    if "#" not in value:
        raise ValueError(
            f"review-pr expects OWNER/REPO#PR_NUMBER, got {value!r}"
        )
    repo_full, pr_str = value.rsplit("#", 1)
    if repo_full.count("/") != 1:
        raise ValueError(
            f"review-pr repo must be owner/repo, got {repo_full!r}"
        )
    try:
        pr_number = int(pr_str)
    except ValueError as exc:
        raise ValueError(
            f"review-pr PR number must be an integer, got {pr_str!r}"
        ) from exc
    owner, repo = repo_full.split("/", 1)
    return owner, repo, pr_number


async def cmd_review_pr(args: argparse.Namespace) -> int:
    if not settings.openrouter_api_key:
        print(
            "error: OPENROUTER_API_KEY is not set in .env",
            file=sys.stderr,
        )
        return 2

    try:
        owner, repo, pr_number = _parse_pr_target(args.pr)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    repo_full = f"{owner}/{repo}"
    repo_name = args.repo_name or repo_full
    mode = WriteMode(settings.write_mode)

    # GitHub credentials needed for fetching the PR (even in shadow mode,
    # because we need to read the PR diff from the API).
    if (
        not settings.github_app_id
        or not settings.github_app_private_key_path
    ):
        print(
            "error: GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH must be "
            "set in .env (needed to fetch the PR from GitHub)",
            file=sys.stderr,
        )
        return 2

    auth = GithubAppAuth.from_files(
        app_id=settings.github_app_id,
        private_key_path=settings.github_app_private_key_path,
    )

    model = args.model or settings.ai_default_model

    async with GithubClient(auth=auth) as gh:
        print(f"Fetching PR #{pr_number} from {repo_full}...")
        pr_info = await gh.get_pr(owner, repo, pr_number)
        pr_files_json = await gh.get_pr_files(owner, repo, pr_number)

    diff_hunks = parse_pr_files(pr_files_json)
    print(
        f"PR #{pr_number}: {pr_info.title} "
        f"({pr_info.changed_files} files, "
        f"+{pr_info.additions}/-{pr_info.deletions})"
    )

    async with OpenRouterClient(
        api_key=settings.openrouter_api_key, default_model=model
    ) as llm:
        async with SessionLocal() as session:
            try:
                result = await run_pr_review(
                    session,
                    repo_name,
                    pr_info,
                    diff_hunks,
                    llm=llm,
                )
            except RepoNotFoundError:
                print(
                    f"warning: repo {repo_name!r} not indexed — "
                    f"reviewing diff without code context",
                    file=sys.stderr,
                )
                # Re-run with a fallback: index doesn't exist but the
                # diff alone is still reviewable. For now, bail.
                return 1
            except PRReviewError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1

    print()
    print(fmt_pr_review_result(result))

    if not args.post:
        return 0

    # --- Posting flow ---
    decision = build_pr_review_decision(
        result, repo_full_name=repo_full, pr_number=pr_number
    )

    print()
    print(f"--- Posting review (WRITE_MODE={mode.value}) ---")
    print(f"target: {repo_full}#{pr_number}")
    print(f"verdict: {result.verdict}")
    print(f"confidence: {decision.confidence:.2f}")

    if mode == WriteMode.SHADOW:
        async with SessionLocal() as session:
            decision_result = await execute_decision(
                decision,
                mode=mode,
                session=session,
                agent="pr_reviewer",
            )
            await session.commit()
        print()
        print(f"outcome: {decision_result.outcome.value}")
        print("(shadow mode — no comment posted)")
        return 0

    async with GithubClient(auth=auth) as gh, SessionLocal() as session:
        decision_result = await execute_decision(
            decision,
            mode=mode,
            client=gh,
            session=session,
            agent="pr_reviewer",
        )
        await session.commit()

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


# ---------------------------------------------------------------------------
# Onboard command + flows
# ---------------------------------------------------------------------------
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


def _parse_target_repo(value: str) -> str:
    """Validate ``owner/repo`` shape (used by ``--create-issues``)."""
    if value.count("/") != 1 or not all(value.split("/")):
        raise ValueError(
            f"--create-issues expects owner/repo, got {value!r}"
        )
    return value


def _print_decision_summary(decision: Decision, result: DecisionResult) -> None:
    """One-line-ish summary per Decision in the ``--create-issues`` loop."""
    title = decision.payload.get("title", "(no title)")
    outcome = result.outcome.value
    side = result.side_effect or {}
    url = side.get("html_url")
    external_id = side.get("id") or side.get("external_id")
    print(f"  [{outcome}] {title}")
    if url:
        print(f"          url: {url}")
    elif external_id is not None:
        print(f"          id:  {external_id}")
    if result.error:
        print(f"          error: {result.error}")


async def cmd_onboard(args: argparse.Namespace) -> int:
    if not settings.openrouter_api_key:
        print(
            "error: OPENROUTER_API_KEY is not set in .env — cannot call the "
            "onboarding LLM",
            file=sys.stderr,
        )
        return 2

    # --- Flag parsing + mutual exclusion ---
    if args.post_to and args.create_issues:
        print(
            "error: --post-to and --create-issues are mutually exclusive",
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

    create_target_repo: str | None = None
    if args.create_issues:
        try:
            create_target_repo = _parse_target_repo(args.create_issues)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    mode = WriteMode(settings.write_mode)

    # --create-issues in comment mode needs a fallback landing issue, since
    # create_issue can't be downgraded in place (no issue exists yet).
    if (
        create_target_repo is not None
        and mode == WriteMode.COMMENT
        and args.fallback_issue is None
    ):
        print(
            "error: --create-issues with WRITE_MODE=comment requires "
            "--fallback-issue N so downgrades have somewhere to land",
            file=sys.stderr,
        )
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

    print(fmt_onboarding_result(result))

    if post_target is None and create_target_repo is None:
        return 0

    if post_target is not None:
        return await _run_post_flow(result, post_target, mode)

    assert create_target_repo is not None  # narrows type for mypy
    return await _run_create_issues_flow(
        result,
        create_target_repo,
        fallback_issue=args.fallback_issue,
        mode=mode,
        max_issues=args.max_issues,
    )


# ---------------------------------------------------------------------------
# --post-to flow (Week 2 Day 7 bridge — single comment)
# ---------------------------------------------------------------------------
async def _run_post_flow(
    result: OnboardingResult,
    post_target: tuple[str, int],
    mode: WriteMode,
) -> int:
    repo_full, issue_number = post_target
    decision = build_onboarding_comment_decision(
        result, repo_full_name=repo_full, issue_number=issue_number
    )

    print()
    print(f"--- Posting flow (WRITE_MODE={mode.value}) ---")
    print(f"target: {repo_full}#{issue_number}")
    print(f"decision confidence: {decision.confidence:.2f}")
    print("evidence:")
    for bullet in decision.evidence:
        print(f"  - {bullet}")

    if mode == WriteMode.SHADOW:
        async with SessionLocal() as session:
            decision_result = await execute_decision(
                decision,
                mode=mode,
                session=session,
                agent="onboarding",
            )
            await session.commit()
        print()
        print(f"outcome: {decision_result.outcome.value}")
        print(
            "(shadow mode — no comment was posted; "
            "flip WRITE_MODE=comment to post for real)"
        )
        return 0

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
    async with GithubClient(auth=auth) as gh, SessionLocal() as session:
        decision_result = await execute_decision(
            decision,
            mode=mode,
            client=gh,
            session=session,
            agent="onboarding",
        )
        await session.commit()

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


# ---------------------------------------------------------------------------
# --create-issues flow (Week 3 Day 4 bridge — one Decision per milestone)
# ---------------------------------------------------------------------------
async def _run_create_issues_flow(
    result: OnboardingResult,
    target_repo: str,
    *,
    fallback_issue: int | None,
    mode: WriteMode,
    max_issues: int = _DEFAULT_MAX_ISSUES,
) -> int:
    decisions = build_onboarding_issue_decisions(
        result,
        target_repo=target_repo,
        fallback_comment_target=fallback_issue,
    )

    print()
    print(f"--- Create-issues flow (WRITE_MODE={mode.value}) ---")
    print(f"target repo: {target_repo}")
    if fallback_issue is not None:
        print(f"fallback issue (for downgrades): #{fallback_issue}")
    print(f"milestones → decisions: {len(decisions)}")

    if not decisions:
        print("(nothing to create — zero milestones with valid findings)")
        return 0

    if len(decisions) > max_issues:
        print(
            f"error: {len(decisions)} decisions exceed the per-invocation "
            f"cap of {max_issues}. Raise --max-issues to override, or "
            f"reduce milestones via the onboarding prompts.",
            file=sys.stderr,
        )
        return 2

    # Non-shadow modes need credentials and a real client.
    gh: GithubClient | None = None
    if mode != WriteMode.SHADOW:
        if (
            not settings.github_app_id
            or not settings.github_app_private_key_path
        ):
            print(
                "error: GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH must be "
                "set in .env when WRITE_MODE is not shadow",
                file=sys.stderr,
            )
            return 2
        auth = GithubAppAuth.from_files(
            app_id=settings.github_app_id,
            private_key_path=settings.github_app_private_key_path,
        )
        gh = GithubClient(auth=auth)

    executed = 0
    deduped = 0
    errors = 0
    try:
        async with SessionLocal() as session:
            for decision in decisions:
                decision_result = await execute_decision(
                    decision,
                    mode=mode,
                    client=gh,
                    session=session,
                    agent="onboarding",
                )
                _print_decision_summary(decision, decision_result)
                if decision_result.outcome == Outcome.EXECUTED:
                    executed += 1
                elif decision_result.outcome == Outcome.DEDUPED:
                    deduped += 1
                elif decision_result.error:
                    errors += 1
            await session.commit()
    finally:
        if gh is not None:
            await gh.aclose()

    print()
    print(
        f"summary: {len(decisions)} decisions  "
        f"executed={executed}  deduped={deduped}  errors={errors}"
    )
    if mode == WriteMode.SHADOW:
        print(
            "(shadow mode — no issues were created; "
            "flip WRITE_MODE=full to create real issues)"
        )
    return 1 if errors else 0
