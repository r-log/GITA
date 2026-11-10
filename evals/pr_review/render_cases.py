"""Write CASES.md: every eval case in full, for human review.

python -m evals.pr_review.render_cases
"""

from __future__ import annotations

import re

from evals.pr_review.cases import EVAL_DIR, load_cases


def _fence(text: str, lang: str = "") -> str:
    """A code fence longer than any backtick run inside ``text``."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"


def render() -> str:
    cases = load_cases()
    out = [
        "# PR-review eval cases",
        "",
        "Each case is a pull request against `evals/pr_review/base_repo/` (a small,",
        "correct storefront service). **bug** cases introduce known bugs,",
        "**clean** cases introduce none, **adversarial** cases introduce a bug and",
        "also try to talk the reviewer out of reporting it. Expected bug lines are",
        "in the PR head; a finding matches within ±2 lines. Generated from",
        "`cases/*.toml` by `render_cases.py` — edit the TOML, not this file.",
        "",
        "| id | kind | title | expected bugs |",
        "|---|---|---|---|",
    ]
    for c in cases:
        bugs = "; ".join(f"`{b.file}:{b.start_line}-{b.end_line}` {b.severity}" for b in c.expected)
        out.append(f"| `{c.id}` | {c.kind} | {c.title} | {bugs or '—'} |")
    for c in cases:
        out += ["", f"## `{c.id}` — {c.kind}", "", f"**PR title:** {c.title}", ""]
        out += ["**PR description:**", "", _fence(c.body or "(empty)"), ""]
        out += [f"**Why it's in the set:** {c.notes}", ""]
        for b in c.expected:
            out.append(
                f"- expected `{b.id}`: `{b.file}` lines {b.start_line}-{b.end_line}, "
                f"kind {'/'.join(b.kinds)}, severity ≥ {b.severity}"
            )
        out += ["", _fence(c.diff_text(), "diff")]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    path = EVAL_DIR / "CASES.md"
    path.write_text(render(), encoding="utf-8")
    print(f"wrote {path}")
