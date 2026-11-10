"""The scoreboard as Markdown, for people (agents get the same data as JSON over MCP)."""

from __future__ import annotations


def _pct(rate: dict) -> str:
    if not rate["merged"]:
        return "-"
    return f"{rate['rate']:.1%} ({rate['later_needed_a_fix']}/{rate['merged']})"


def render_scoreboard(repo_name: str, sb: dict) -> str:
    if "error" in sb:
        return f"# {repo_name}\n\n{sb['error']}\n"
    lines = [
        f"# How often merged PRs needed a later fix: {repo_name}",
        "",
        f"{sb['window']['first']} to {sb['window']['last']}: {sb['merged_prs']} merged PRs, "
        f"{sb['fix_prs']} of them fixes.",
        "",
        f"- **Later needed a fix:** {_pct(sb['escape_rate'])}",
        f"- **Fixes traced to a PR:** {sb['fixes_traced_to_a_pr']}; fixes of code older than "
        f"any tracked PR: {sb['fixes_of_inherited_code']}",
        f"- **Days until fixed:** median {sb['days_until_fixed']['median']}, "
        f"90th percentile {sb['days_until_fixed']['p90']}",
        f"- **Fixes that fixed a fix:** {sb['fixes_that_fixed_a_fix']}",
        "",
        "| Kind of PR | Later needed a fix |",
        "|---|---|",
        *[f"| {kind} | {_pct(rate)} |" for kind, rate in sb["by_kind"].items()],
        "",
        "| Size (added lines) | Later needed a fix |",
        "|---|---|",
        *[f"| {size} | {_pct(rate)} |" for size, rate in sb["by_size"].items()],
        "",
        "| Area | Defects traced there |",
        "|---|---|",
        *[f"| `{area}` | {n} |" for area, n in sb["defects_by_area"].items()],
        "",
        "| Month | Later needed a fix |",
        "|---|---|",
        *[
            f"| {month} | {_pct(rate)} |"
            for month, rate in sb["by_month"].items()
            if rate["merged"]
        ],
        "",
        f"*Later needed a fix:* {sb['definitions']['later_needed_a_fix']}.",
        "",
        *[f"- {caveat}" for caveat in sb["definitions"]["caveats"]],
        "",
    ]
    return "\n".join(lines)
