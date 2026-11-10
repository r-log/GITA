"""Mine backtest cases from a repository's history: PRs that a later fix PR repaired.

    python -m evals.pr_backtest.mine /path/to/full/clone --out evals/pr_backtest/cases.json

For every merged PR whose branch is ``fix/...``:

1. Take the fix's own changes — from the merge base, as GitHub's "Files
   changed" shows them. (Diffing against the merge's first parent would
   attribute to the fix everything that merged to main while its branch
   was open.)
2. Blame each C/C++ line the fix modified or removed, at the fix's merge
   base, and map the blamed commit to the PR that merged it (SZZ).
3. If at least half of the fix's product-code lines (outside tests) come
   from one earlier PR, that PR introduced the defect: a case.
4. Ground truth is where those lines sat in the introducing PR's head, so
   a reviewer of that PR could have pointed at them.

Several fixes of the same PR become one case with several expected bugs.
The output is data only: whether each defect was visible from the code at
review time is labelled by hand in ``labels.toml``, before any run.
"""

from __future__ import annotations

import argparse
import collections
import difflib
import json
import re
import subprocess
from pathlib import Path

SOURCE = re.compile(r"\.(c|cc|cpp|cxx|h|hh|hpp|hxx|inl)$")
MERGE = re.compile(r"Merge pull request #(\d+) from [^/]+/(.+)")
TEST_PREFIXES = ("src/tests/",)
MIN_SHARE = 0.5


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _show(repo: Path, rev: str, path: str) -> str | None:
    try:
        return _git(repo, "show", f"{rev}:{path}")
    except subprocess.CalledProcessError:
        return None


def merged_prs(repo: Path) -> list[dict]:
    prs = []
    for line in _git(
        repo, "log", "--first-parent", "--merges", "--format=%H|%P", "HEAD"
    ).splitlines():
        sha, parents = line.split("|")
        subject, _, body = _git(repo, "log", "-1", "--format=%s%n%b", sha).partition("\n")
        m = MERGE.match(subject)
        ps = parents.split()
        if not m or len(ps) != 2:
            continue
        prs.append(
            {
                "pr": int(m[1]),
                "branch": m[2],
                "title": body.strip().splitlines()[0] if body.strip() else m[2],
                "merge": sha,
                "base": _git(repo, "merge-base", ps[0], ps[1]).strip(),
                "head": ps[1],
            }
        )
    return prs


def _owner_map(repo: Path, prs: list[dict]) -> dict[str, int]:
    owner: dict[str, int] = {}
    for p in reversed(prs):  # oldest first: a commit belongs to the PR that merged it first
        for c in _git(repo, "rev-list", f"{p['base']}..{p['head']}").split():
            owner.setdefault(c, p["pr"])
    return owner


def _blamed_lines(repo: Path, fix: dict, owner: dict[str, int]) -> list[dict]:
    """Every base-side line the fix modified or removed, with the PR that wrote it."""
    out = []
    path = None
    for line in _git(repo, "diff", "-U0", "--no-color", fix["base"], fix["head"]).splitlines():
        if line.startswith("--- "):
            path = line[6:] if line.startswith("--- a/") else None
            continue
        hunk = re.match(r"@@ -(\d+)(?:,(\d+))? \+", line)
        if not (hunk and path and SOURCE.search(path)):
            continue
        start, count = int(hunk[1]), int(hunk[2] if hunk[2] is not None else 1)
        if count == 0:
            continue
        porcelain = _git(
            repo,
            "blame",
            "--porcelain",
            "-L",
            f"{start},{start + count - 1}",
            fix["base"],
            "--",
            path,
        )
        for commit, final in re.findall(r"^([0-9a-f]{40}) \d+ (\d+)", porcelain, re.M):
            out.append({"file": path, "line": int(final), "pr": owner.get(commit)})
    return out


def _map_to_head(repo: Path, intro: dict, fix: dict, path: str, lines: list[int]) -> list[int]:
    """Where lines of ``path`` at the fix's base sat in the introducing PR's head."""
    head = _show(repo, intro["head"], path)
    later = _show(repo, fix["base"], path)
    if head is None or later is None:
        return []
    a, b = head.splitlines(), later.splitlines()
    mapping: dict[int, int] = {}
    for tag, i1, _i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(j2 - j1):
                mapping[j1 + k + 1] = i1 + k + 1
    return sorted({mapping[n] for n in lines if n in mapping})


def mine(repo: Path) -> list[dict]:
    prs = merged_prs(repo)
    by_number = {p["pr"]: p for p in prs}
    owner = _owner_map(repo, prs)
    cases: dict[int, dict] = {}
    for fix in prs:
        if not fix["branch"].startswith("fix/"):
            continue
        product = [
            b for b in _blamed_lines(repo, fix, owner) if not b["file"].startswith(TEST_PREFIXES)
        ]
        if not product:
            continue
        counts = collections.Counter(b["pr"] for b in product if b["pr"] is not None)
        if not counts:
            continue
        intro_pr, n = counts.most_common(1)[0]
        if n / len(product) < MIN_SHARE or intro_pr == fix["pr"]:
            continue
        intro = by_number[intro_pr]
        by_file: dict[str, list[int]] = collections.defaultdict(list)
        for b in product:
            if b["pr"] == intro_pr:
                by_file[b["file"]].append(b["line"])
        truth = []
        for path, lines in sorted(by_file.items()):
            mapped = _map_to_head(repo, intro, fix, path, lines)
            if mapped:
                truth.append({"file": path, "lines": mapped})
        if not truth:
            continue
        case = cases.setdefault(
            intro_pr,
            {
                "id": f"pr{intro_pr}",
                "pr": intro_pr,
                "title": intro["title"],
                "base": intro["base"],
                "head": intro["head"],
                "bugs": [],
            },
        )
        case["bugs"].append(
            {
                "id": f"fix{fix['pr']}",
                "fix_pr": fix["pr"],
                "fix_title": fix["title"],
                "fix_base": fix["base"],
                "fix_head": fix["head"],
                "share": round(n / len(product), 2),
                "truth": truth,
            }
        )
    return sorted(cases.values(), key=lambda c: c["pr"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("repo", type=Path, help="a clone with enough history")
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "cases.json")
    parser.add_argument("--github", default="r-log/mangosthree-server")
    args = parser.parse_args()
    cases = mine(args.repo.resolve())
    payload = {
        "repo": args.github,
        "mined_at_head": _git(args.repo, "rev-parse", "HEAD").strip(),
        "cases": cases,
    }
    args.out.write_text(json.dumps(payload, indent=1) + "\n")
    bugs = sum(len(c["bugs"]) for c in cases)
    print(f"wrote {args.out}: {len(cases)} PRs, {bugs} later-fixed defects")


if __name__ == "__main__":
    main()
