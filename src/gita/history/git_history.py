"""Read a repository's merged pull requests and trace fixes to their causes, from git alone.

No GitHub API, no model calls: everything here comes from the default
branch's first-parent history of a local clone.

* ``list_merged_prs`` finds merged PRs: GitHub merge commits ("Merge pull
  request #N from owner/branch") and squash merges ("Title (#N)"). A PR's
  own changes run from its merge base to its head, as GitHub's "Files
  changed" shows them; diffing against the merge's first parent would
  credit it with everything that merged while its branch was open.
* ``blame_fix`` blames each product-code line a fix PR modified or removed,
  at the fix's merge base (where a fix only inserts code, the lines on
  either side of the insertion): which commits wrote what the fix had to
  change.
* ``link_fix`` turns those commits into the PRs that introduced the
  defect, with each one's share of the blamed lines and where the lines
  sat in that PR's head (following renames in between).

This is the SZZ approach. It is evidence, not proof: a fix that rewrites
code for another reason, or code a PR only moved, is credited to that PR.
Shares and line ranges are kept so a reader can judge.
"""

from __future__ import annotations

import collections
import difflib
import functools
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from gita.indexer.walker import LANGUAGE_BY_EXT, TEST_DIRS

_MERGE = re.compile(r"^Merge pull request #(\d+) from [^/\s]+/(\S+)")
_SQUASH = re.compile(r"\(#(\d+)\)\s*$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+")
_BLAME = re.compile(r"^([0-9a-f]{40}) \d+ (\d+)", re.M)
_SEP = "\x1f"

# Branch prefixes and conventional-commit prefixes, by kind of change.
_KINDS = {
    "fix": ("fix", "bugfix", "hotfix", "bug"),
    "feature": ("feat", "feature"),
    "refactor": ("refactor", "refactoring"),
    "docs": ("docs", "doc"),
    "test": ("test", "tests"),
    "chore": ("chore", "ci", "build", "deps", "tidy", "cleanup", "bloat", "style", "perf"),
}
# "fix: ...", "fix(cart): ...", or a bracketed "[fix] ..." (only kind words
# count: "[Quests] ..." names an area, not a kind).
_CONVENTIONAL = re.compile(r"^\s*(?:\[([a-z]+)\]|([a-z]+)(?:\([^)]*\))?!?:)", re.I)


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise GitError(f"git {' '.join(args[:3])}: {exc.stderr.strip()[:300]}") from exc


def show(repo: Path, rev: str, path: str) -> str | None:
    try:
        return git(repo, "show", f"{rev}:{path}")
    except GitError:
        return None


def is_shallow(repo: Path) -> bool:
    return git(repo, "rev-parse", "--is-shallow-repository").strip() == "true"


def classify(branch: str | None, title: str) -> str:
    """fix / feature / refactor / docs / test / chore / other."""
    word = None
    if branch and "/" in branch:
        word = branch.split("/", 1)[0].lower()
    elif m := _CONVENTIONAL.match(title):
        word = (m.group(1) or m.group(2)).lower()
    for kind, words in _KINDS.items():
        if word in words:
            return kind
    return "other"


def is_product_path(path: str) -> bool:
    """A file in a language GITA indexes, outside test directories."""
    p = Path(path)
    if LANGUAGE_BY_EXT.get(p.suffix.lower()) is None:
        return False
    return not any(part in TEST_DIRS for part in p.parts[:-1])


@dataclass(frozen=True)
class PRFile:
    path: str
    old_path: str | None  # set when git sees a rename
    additions: int
    deletions: int


@dataclass
class MergedPR:
    number: int
    title: str
    branch: str | None
    kind: str
    merge_sha: str
    base_sha: str
    head_sha: str
    merged_at: datetime
    author: str
    files: list[PRFile] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)


def _pr_files(repo: Path, base: str, head: str) -> list[PRFile]:
    stats: dict[str, tuple[int, int]] = {}
    for line in git(repo, "diff", "--numstat", "-M50%", base, head).splitlines():
        added, deleted, path = line.split("\t", 2)
        if " => " in path:  # rename in numstat form: a/{x => y}/b
            path = _renamed_target(path)
        stats[path] = (int(added) if added != "-" else 0, int(deleted) if deleted != "-" else 0)
    files = []
    for line in git(repo, "diff", "--name-status", "-M50%", base, head).splitlines():
        parts = line.split("\t")
        old = parts[1] if parts[0].startswith("R") and len(parts) == 3 else None
        path = parts[-1]
        added, deleted = stats.get(path, (0, 0))
        files.append(PRFile(path=path, old_path=old, additions=added, deletions=deleted))
    return files


def _renamed_target(numstat_path: str) -> str:
    m = re.match(r"(.*)\{(.*) => (.*)\}(.*)", numstat_path)
    if m:
        return (m.group(1) + m.group(3) + m.group(4)).replace("//", "/")
    return numstat_path.split(" => ", 1)[1]


def list_merged_prs(repo: Path, since: str | None = None) -> list[MergedPR]:
    """Merged PRs on the default branch's first-parent line, oldest first.

    ``since``: a commit already processed; only later merges are returned.

    A fork's history carries its upstream's commits, and upstream squash
    merges end in "(#N)" with the *upstream's* PR numbers. So when the
    repository merges with GitHub merge commits, squash-style commits older
    than its first merge commit are inherited history, not its PRs; and if
    a number still repeats, the newest occurrence wins.
    """
    rng = f"{since}..HEAD" if since else "HEAD"
    fmt = _SEP.join(("%H", "%P", "%cI", "%s", "%b")) + "\x1e"
    out = git(repo, "log", "--first-parent", "--reverse", f"--format={fmt}", rng)
    candidates = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, parents, date, subject, body = (record.split(_SEP) + [""] * 5)[:5]
        parents = parents.split()
        merge = _MERGE.match(subject)
        squash = _SQUASH.search(subject)
        if merge and len(parents) == 2:
            title = body.strip().splitlines()[0] if body.strip() else merge.group(2)
            candidates.append((int(merge.group(1)), merge.group(2), title, sha, parents, date))
        elif squash and len(parents) == 1:
            title = _SQUASH.sub("", subject).strip()
            candidates.append((int(squash.group(1)), None, title, sha, parents, date))

    merges = [c for c in candidates if c[1] is not None]
    if merges and since is None:
        first = datetime.fromisoformat(merges[0][5])
        candidates = [
            c for c in candidates if c[1] is not None or datetime.fromisoformat(c[5]) >= first
        ]
    newest = {c[0]: c for c in candidates}  # oldest first, so later ones overwrite
    kept = [c for c in candidates if newest[c[0]] is c]

    prs = []
    for number, branch, title, sha, parents, date in kept:
        if branch is not None:
            base = git(repo, "merge-base", parents[0], parents[1]).strip()
            head = parents[1]
            commits = git(repo, "rev-list", f"{base}..{head}").split()
        else:
            base, head, commits = parents[0], sha, [sha]
        prs.append(
            MergedPR(
                number=number,
                title=title,
                branch=branch,
                kind=classify(branch, title),
                merge_sha=sha,
                base_sha=base,
                head_sha=head,
                merged_at=datetime.fromisoformat(date),
                author=git(repo, "log", "-1", "--format=%an", head).strip(),
                files=_pr_files(repo, base, head),
                commits=commits,
            )
        )
    return prs


@dataclass(frozen=True)
class BlamedLine:
    path: str  # at the fix's base
    line: int
    commit: str


def blame_fix(repo: Path, fix: MergedPR) -> list[BlamedLine]:
    """Each product-code line the fix modified or removed, with the commit that wrote it."""
    out: list[BlamedLine] = []
    path = None
    for row in git(repo, "diff", "-U0", "--no-color", fix.base_sha, fix.head_sha).splitlines():
        if row.startswith("--- "):
            path = row[6:] if row.startswith("--- a/") else None
            continue
        hunk = _HUNK.match(row)
        if not (hunk and path and is_product_path(path)):
            continue
        start, count = int(hunk.group(1)), int(hunk.group(2) if hunk.group(2) is not None else 1)
        if count == 0:
            # A pure insertion after line `start` changes no line, but the
            # defect is where the code was missing: blame its neighbours.
            first, last = max(start, 1), start + 1
        else:
            first, last = start, start + count - 1
        try:
            porcelain = git(
                repo, "blame", "--porcelain", "-L", f"{first},{last}", fix.base_sha, "--", path
            )
        except GitError:  # the line after is past the end of the file
            porcelain = git(
                repo, "blame", "--porcelain", "-L", f"{first},{first}", fix.base_sha, "--", path
            )
        for commit, final in _BLAME.findall(porcelain):
            out.append(BlamedLine(path=path, line=int(final), commit=commit))
    return out


@functools.lru_cache(maxsize=256)
def _renames(repo: Path, older: str, newer: str) -> dict[str, str]:
    """Path at ``newer`` -> its path at ``older``, for files git sees as renamed."""
    out = git(repo, "diff", "--name-status", "-M50%", older, newer)
    return {
        parts[2]: parts[1]
        for parts in (line.split("\t") for line in out.splitlines())
        if parts[0].startswith("R") and len(parts) == 3
    }


def map_to_head(
    repo: Path, intro_head: str, fix_base: str, path: str, lines: Iterable[int]
) -> tuple[str, list[int]]:
    """Where lines of ``path`` at the fix's base sat in the introducing PR's head."""
    head_path = path
    head = show(repo, intro_head, path)
    if head is None:
        head_path = _renames(repo, intro_head, fix_base).get(path, path)
        head = show(repo, intro_head, head_path)
    later = show(repo, fix_base, path)
    if head is None or later is None:
        return head_path, []
    a, b = head.splitlines(), later.splitlines()
    mapping: dict[int, int] = {}
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(j2 - j1):
                mapping[j1 + k + 1] = i1 + k + 1
    return head_path, sorted({mapping[n] for n in lines if n in mapping})


@dataclass(frozen=True)
class DefectLink:
    fix_pr: int
    introducing_pr: int | None  # None: code older than any tracked PR
    share: float  # of the fix's blamed product lines
    lines: list[dict]  # [{"file": path in the introducing PR's head, "lines": [...]}]


def link_fix(
    repo: Path,
    fix: MergedPR,
    owner_of: Callable[[Iterable[str]], dict[str, int]],
    head_of: Callable[[int], str | None],
) -> list[DefectLink]:
    """The PRs whose code this fix changed, each with its share of the blamed lines.

    ``owner_of`` maps commits to the PR that merged them; ``head_of`` gives
    a PR's head commit. Lines older than every tracked PR are one link with
    ``introducing_pr=None``.
    """
    blamed = blame_fix(repo, fix)
    if not blamed:
        return []
    owners = owner_of({b.commit for b in blamed})
    by_pr: dict[int | None, list[BlamedLine]] = collections.defaultdict(list)
    for b in blamed:
        pr = owners.get(b.commit)
        if pr != fix.number:
            by_pr[pr].append(b)
    links = []
    for pr, rows in by_pr.items():
        share = round(len(rows) / len(blamed), 3)
        lines: list[dict] = []
        head = head_of(pr) if pr is not None else None
        by_file: dict[str, list[int]] = collections.defaultdict(list)
        for b in rows:
            by_file[b.path].append(b.line)
        for path, nums in sorted(by_file.items()):
            if head is None:
                lines.append({"file": path, "lines": sorted(set(nums)), "at": "fix base"})
                continue
            head_path, mapped = map_to_head(repo, head, fix.base_sha, path, nums)
            if mapped:
                lines.append({"file": head_path, "lines": mapped})
        links.append(DefectLink(fix.number, pr, share, lines))
    return sorted(links, key=lambda link: -link.share)
