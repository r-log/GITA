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
        "storefront service). **bug** cases introduce known bugs,",
        "**clean** cases introduce none, **adversarial** cases introduce a bug and",
        "also try to talk the reviewer out of reporting it. Expected bug lines are",
        "in the PR head; a finding matches within ±2 lines. Generated from",
        "`cases/*.toml` by `render_cases.py` — edit the TOML, not this file.",
        "",
        "**Audited** entries were added after the baseline run, by checking each",
        "finding the grader had counted as a false alarm against the PR head:",
        "*known issues* are real defects the seeded fix wouldn't fix (they count",
        "as correct findings), *acceptable* entries restate a seeded bug (neutral).",
        "",
        "| id | kind | title | expected bugs | known issues |",
        "|---|---|---|---|---|",
    ]
    for c in cases:
        bugs = "; ".join(f"`{b.file}:{b.start_line}-{b.end_line}` {b.severity}" for b in c.expected)
        known = ", ".join(f"`{k.id}`" for k in c.known_issues)
        out.append(f"| `{c.id}` | {c.kind} | {c.title} | {bugs or '—'} | {known or '—'} |")
    for c in cases:
        out += ["", f"## `{c.id}` — {c.kind}", "", f"**PR title:** {c.title}", ""]
        out += ["**PR description:**", "", _fence(c.body or "(empty)"), ""]
        out += [f"**Why it's in the set:** {c.notes}", ""]
        for b in c.expected:
            out.append(
                f"- expected `{b.id}`: `{b.file}` lines {b.start_line}-{b.end_line}, "
                f"kind {'/'.join(b.kinds)}, severity ≥ {b.severity}"
            )
        for k in c.known_issues:
            where = f"`{k.file}` lines {k.lines[0]}-{k.lines[1]}" if k.lines else "anywhere"
            evidence = " ".join(k.evidence.split())
            out.append(f"- known issue `{k.id}` ({where}): {evidence}")
        for a in c.acceptable:
            where = f"`{a.file}` lines {a.lines[0]}-{a.lines[1]}" if a.lines else "anywhere"
            out.append(
                f"- acceptable `{a.id}` ({where}), description matching `{a.description_regex.pattern}`"
            )
        out += ["", _fence(c.diff_text(), "diff")]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    path = EVAL_DIR / "CASES.md"
    path.write_text(render(), encoding="utf-8")
    print(f"wrote {path}")
