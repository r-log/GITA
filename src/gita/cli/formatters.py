"""Plain-text formatters for CLI output.

Pure functions: each takes a result dataclass and returns a formatted
string. No I/O, no side effects, no imports beyond the types they format.
"""
from __future__ import annotations

from pathlib import Path

from gita.agents.test_generator import TestGenerationResult
from gita.agents.types import OnboardingResult, PRReviewResult
from gita.db.models import Repo
from gita.indexer.ingest import IngestResult
from gita.views.concept import ConceptResult
from gita.views.history import HistoryResult
from gita.views.load_bearing import LoadBearingResult
from gita.views.neighborhood import NeighborhoodResult
from gita.views.symbol import SymbolResult


def fmt_ingest(name: str, root: Path, elapsed: float, result: IngestResult) -> str:
    resolved_pct = (
        (result.edges_resolved / result.edges_total * 100)
        if result.edges_total
        else 0.0
    )
    mode_label = {
        "full": "full re-index",
        "incremental": "incremental update",
        "noop": "no changes detected",
    }.get(result.mode, result.mode)

    lines = [
        f"Indexed {name!r} ({mode_label})",
        f"  root:      {root}",
    ]
    if result.mode == "noop":
        lines.append(f"  head:      {result.head_sha or '<not a git repo>'}")
        lines.append(f"  elapsed:   {elapsed:.2f}s")
        return "\n".join(lines)

    if result.mode == "incremental" and result.files_deleted > 0:
        lines.append(
            f"  files:     {result.files_indexed} updated, "
            f"{result.files_deleted} deleted"
        )
    else:
        lines.append(f"  files:     {result.files_indexed}")
    lines.extend([
        f"  functions: {result.functions_extracted}",
        f"  classes:   {result.classes_extracted}",
        f"  imports:   {result.edges_total} "
        f"({result.edges_resolved} resolved, "
        f"{result.edges_total - result.edges_resolved} unresolved, "
        f"{resolved_pct:.0f}% rate)",
    ])
    if result.files_embedded:
        lines.append(f"  embedded:  {result.files_embedded}")
    lines.extend([
        f"  head:      {result.head_sha or '<not a git repo>'}",
        f"  elapsed:   {elapsed:.2f}s",
    ])
    return "\n".join(lines)


def fmt_repos(rows: list[tuple[Repo, int, int]]) -> str:
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


def fmt_stats(
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


def fmt_symbol_result(result: SymbolResult) -> str:
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


def fmt_neighborhood_result(result: NeighborhoodResult) -> str:
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


def fmt_load_bearing_result(result: LoadBearingResult) -> str:
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


def fmt_onboarding_result(result: OnboardingResult) -> str:
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


def fmt_concept_result(result: ConceptResult) -> str:
    if not result.matches:
        return f"No matches for {result.query!r} in {result.repo_name}"

    header = (
        f"{result.total_matches} match"
        f"{'es' if result.total_matches != 1 else ''} "
        f"for {result.query!r} in {result.repo_name}"
    )
    if result.total_matches > len(result.matches):
        header += f" (showing top {len(result.matches)})"

    lines = [header, ""]
    for i, match in enumerate(result.matches, start=1):
        lines.append(
            f"  {i}. {match.file_path}  "
            f"({match.language}, {match.line_count} lines, "
            f"rank {match.rank:.3f})"
        )
        if match.matching_symbols:
            sym_names = ", ".join(s.name for s in match.matching_symbols[:5])
            lines.append(f"     symbols: {sym_names}")
        if match.headline:
            # Clean up the headline for CLI display.
            clean = match.headline.replace("\n", " ").strip()
            if len(clean) > 120:
                clean = clean[:117] + "..."
            lines.append(f"     ...{clean}")
        lines.append("")
    return "\n".join(lines).rstrip()


def fmt_pr_review_result(result: PRReviewResult) -> str:
    lines = [
        f"PR Review: #{result.pr_number} — {result.pr_title}",
        "",
        f"verdict: {result.verdict}",
        f"summary: {result.summary}",
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
    lines.append(f"confidence: {result.confidence:.2f}")
    return "\n".join(lines)


def fmt_history_result(result: HistoryResult) -> str:
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


def fmt_test_generation_result(result: TestGenerationResult) -> str:
    verdict = "verified" if result.verified else "NOT verified"
    lines = [
        f"Test generation: {result.target_file}",
        "",
        f"output:     {result.test_file_path}",
        f"size:       {len(result.test_content)} bytes, "
        f"{len(result.test_content.splitlines())} lines",
        f"verdict:    {verdict}",
    ]
    if result.llm_model:
        lines.append(f"model:      {result.llm_model}")

    if result.covered_symbols:
        lines.append("")
        lines.append(f"covered symbols ({len(result.covered_symbols)}):")
        for sym in result.covered_symbols:
            lines.append(f"  - {sym}")

    if result.verification_errors:
        lines.append("")
        lines.append(f"verification errors ({len(result.verification_errors)}):")
        for err in result.verification_errors:
            # Collapse multi-line errors into a single bullet so the block
            # scans at a glance; subprocess output can be 10+ lines.
            first = err.splitlines()[0] if err else ""
            lines.append(f"  - {first}")

    if result.notes:
        lines.append("")
        lines.append("notes:")
        lines.append(f"  {result.notes}")

    lines.append("")
    lines.append(
        f"confidence: {result.confidence:.2f} "
        f"(llm={result.llm_confidence:.2f}, verified={result.verified})"
    )
    return "\n".join(lines)
