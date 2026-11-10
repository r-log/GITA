"""Questions an agent (or a person) asks the repository's memory.

Every answer is built from the stored history (gita.history.sync) and the
code index, with no model calls. Paths are repository-relative; a path that
names a directory matches everything under it.

Defect links come from line tracing (SZZ): a fix PR changed lines another
PR wrote. That is strong evidence the earlier PR introduced the defect, not
proof; each answer carries the share of the fix's lines and the exact
lines, so the reader can judge.
"""

from __future__ import annotations

import collections
import re
import statistics
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from gita.db.models import CodeIndex, DefectLink, HistoryPR, HistoryPRFile, ImportEdge, Repo

# A fix is attributed to the PR holding at least this share of its lines.
PRIMARY_SHARE = 0.5
_DIFF_FILE = re.compile(r"^\+\+\+ b/(.+)$", re.M)


class NotSynced(LookupError):
    """The repository has no history memory yet: run ``gita history sync``."""


@dataclass
class Memory:
    """The stored history of one repository, loaded once per question."""

    repo: Repo
    prs: dict[int, HistoryPR]
    links: list[DefectLink]
    fix_files: dict[int, set[str]]

    def rel(self, path: str) -> str:
        """Repository-relative, forward slashes; absolute paths inside the clone are cut."""
        p = path.replace("\\", "/").strip()
        root = self.repo.root_path.rstrip("/") + "/"
        if p.startswith(root):
            p = p[len(root) :]
        return p.lstrip("./").rstrip("/") if p not in (".", "./") else ""


async def load(session: AsyncSession, repo_name: str) -> Memory:
    repo = (await session.execute(select(Repo).where(Repo.name == repo_name))).scalar_one_or_none()
    if repo is None or repo.history_synced_sha is None:
        raise NotSynced(f"no history for {repo_name!r}: run `gita history sync <clone>` first")
    prs = {
        p.number: p
        for p in (await session.execute(select(HistoryPR).where(HistoryPR.repo_id == repo.id)))
        .scalars()
        .all()
    }
    links = list(
        (await session.execute(select(DefectLink).where(DefectLink.repo_id == repo.id)))
        .scalars()
        .all()
    )
    fix_numbers = {link.fix_pr for link in links}
    fix_files: dict[int, set[str]] = collections.defaultdict(set)
    if fix_numbers:
        rows = await session.execute(
            select(HistoryPRFile.pr_number, HistoryPRFile.path).where(
                HistoryPRFile.repo_id == repo.id, HistoryPRFile.pr_number.in_(fix_numbers)
            )
        )
        for number, path in rows.all():
            fix_files[number].add(path)
    return Memory(repo, prs, links, fix_files)


def _under(path: str, target: str) -> bool:
    return not target or path == target or path.startswith(target + "/")


def _pr_ref(pr: HistoryPR | None, number: int | None) -> dict | None:
    if number is None:
        return None
    if pr is None:
        return {"number": number}
    return {
        "number": pr.number,
        "title": pr.title,
        "kind": pr.kind,
        "merged": pr.merged_at.date().isoformat(),
    }


def _ranges(lines: list[int]) -> str:
    """[189, 190, 191, 262] -> "189-191, 262": compact for a reader's (or agent's) budget."""
    out: list[str] = []
    for n in sorted(set(lines)):
        if out and "-" not in out[-1].split(", ")[-1] and int(out[-1]) == n - 1:
            out[-1] = f"{out[-1]}-{n}"
        elif out and "-" in out[-1] and int(out[-1].rsplit("-", 1)[1]) == n - 1:
            out[-1] = f"{out[-1].rsplit('-', 1)[0]}-{n}"
        else:
            out.append(str(n))
    return ", ".join(out)


def _where(link: DefectLink) -> list[dict]:
    return [{"file": e["file"], "lines": _ranges(e["lines"])} for e in link.lines]


def _bug(mem: Memory, link: DefectLink) -> dict:
    fix, intro = mem.prs.get(link.fix_pr), mem.prs.get(link.introducing_pr or -1)
    days = (fix.merged_at - intro.merged_at).days if fix and intro else None
    return {
        "what_was_fixed": fix.title if fix else None,
        "fix_pr": _pr_ref(fix, link.fix_pr),
        "introduced_by": _pr_ref(intro, link.introducing_pr),
        "days_until_fixed": days,
        "share_of_fix": link.share,
        "where_in_introducing_pr": _where(link),
    }


def _bug_files(mem: Memory, link: DefectLink) -> set[str]:
    return {entry["file"] for entry in link.lines} | mem.fix_files.get(link.fix_pr, set())


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------
def past_bugs(
    mem: Memory, *, path: str | None = None, text: str | None = None, limit: int = 10
) -> dict:
    """Defects fixed in this area (or matching words), most recent fix first."""
    target = mem.rel(path) if path else None
    words = [w.lower() for w in re.findall(r"\w+", text or "")]
    hits = []
    for link in mem.links:
        if link.introducing_pr is None or link.share < PRIMARY_SHARE / 2:
            continue
        if target is not None and not any(_under(f, target) for f in _bug_files(mem, link)):
            continue
        if words:
            titles = " ".join(
                (mem.prs[n].title if n in mem.prs else "")
                for n in (link.fix_pr, link.introducing_pr)
            ).lower()
            if not all(w in titles for w in words):
                continue
        hits.append(link)
    hits.sort(
        key=lambda link: mem.prs[link.fix_pr].merged_at if link.fix_pr in mem.prs else datetime.min,
        reverse=True,
    )
    return {
        "query": {"path": target, "text": text},
        "found": len(hits),
        "bugs": [_bug(mem, link) for link in hits[:limit]],
        "note": "Traced from later fixes (SZZ): evidence, not proof. Read the lines before relying on it.",
    }


def lessons(mem: Memory, paths: list[str], *, limit: int = 8) -> dict:
    """What went wrong before in these files, then their directories, then the parents."""
    targets = [mem.rel(p) for p in paths if p]
    seen: set[int] = set()
    out = []
    for depth in range(0, 4):
        scopes = {
            str(PurePosixPath(t).parents[depth - 1]) if depth else t
            for t in targets
            if depth == 0 or len(PurePosixPath(t).parents) >= depth
        }
        scopes.discard(".")
        for link in sorted(mem.links, key=lambda link: -link.share):
            if link.introducing_pr is None or link.fix_pr in seen or link.share < PRIMARY_SHARE / 2:
                continue
            files = _bug_files(mem, link)
            scope = next((s for s in scopes if any(_under(f, s) for f in files)), None)
            if scope is None:
                continue
            seen.add(link.fix_pr)
            fix = mem.prs.get(link.fix_pr)
            out.append(
                {
                    "lesson": fix.title if fix else f"fix #{link.fix_pr}",
                    "scope": scope,
                    "match": ["same file", "same directory", "parent directory", "grandparent"][
                        depth
                    ],
                    "fix_pr": link.fix_pr,
                    "introduced_by": link.introducing_pr,
                }
            )
        if len(out) >= limit:
            break
    return {"paths": targets, "lessons": out[:limit]}


async def file_history(session: AsyncSession, mem: Memory, path: str, *, limit: int = 10) -> dict:
    target = mem.rel(path)
    clause = or_(
        HistoryPRFile.path == target,
        HistoryPRFile.path.like(target + "/%"),
        HistoryPRFile.old_path == target,
    )
    rows = (
        await session.execute(
            select(HistoryPRFile.pr_number, HistoryPRFile.additions, HistoryPRFile.deletions)
            .where(HistoryPRFile.repo_id == mem.repo.id)
            .where(clause)
        )
    ).all()
    numbers = sorted(
        {r.pr_number for r in rows},
        key=lambda n: mem.prs[n].merged_at if n in mem.prs else datetime.min,
        reverse=True,
    )
    kinds = collections.Counter(mem.prs[n].kind for n in numbers if n in mem.prs)
    authors = collections.Counter(mem.prs[n].author for n in numbers if n in mem.prs)
    introduced = [
        link
        for link in mem.links
        if link.introducing_pr is not None
        and link.share >= PRIMARY_SHARE / 2
        and any(_under(entry["file"], target) for entry in link.lines)
    ]
    return {
        "path": target,
        "merged_prs": len(numbers),
        "by_kind": dict(kinds),
        "defects_traced_here": len(introduced),
        "recent_prs": [_pr_ref(mem.prs.get(n), n) for n in numbers[:limit]],
        "authors": [name for name, _ in authors.most_common(5)],
    }


async def impact(session: AsyncSession, mem: Memory, target: str, *, limit: int = 20) -> dict:
    """What depends on a file (its includers/importers), or where a symbol is defined and used."""
    repo_id = mem.repo.id
    indexed = await session.scalar(
        select(func.count()).select_from(CodeIndex).where(CodeIndex.repo_id == repo_id)
    )
    if not indexed:
        return {"target": target, "error": "the code isn't indexed: run `gita index <clone>`"}
    rel = mem.rel(target)
    if "/" in rel or "." in PurePosixPath(rel).name:
        importers = (
            (
                await session.execute(
                    select(ImportEdge.src_file)
                    .where(ImportEdge.repo_id == repo_id, ImportEdge.dst_file == rel)
                    .order_by(ImportEdge.src_file)
                )
            )
            .scalars()
            .all()
        )
        row = (
            await session.execute(
                select(CodeIndex).where(CodeIndex.repo_id == repo_id, CodeIndex.file_path == rel)
            )
        ).scalar_one_or_none()
        symbols = []
        if row is not None and row.structure:
            for sym in row.structure.get("classes", []) + row.structure.get("functions", []):
                symbols.append(
                    {
                        "name": sym["name"],
                        "kind": sym["kind"],
                        "class": sym.get("parent_class"),
                        "lines": [sym["start_line"], sym["end_line"]],
                    }
                )
        return {
            "target": rel,
            "kind": "file",
            "included_or_imported_by": len(importers),
            "dependents": list(importers[:limit]),
            "symbols": symbols[:50],
        }

    rows = (
        await session.execute(
            select(CodeIndex.file_path, CodeIndex.structure)
            .where(
                CodeIndex.repo_id == repo_id,
                text(
                    "to_tsvector('simple', COALESCE(content, '')) @@ plainto_tsquery('simple', :sym)"
                ),
            )
            .params(sym=target)
        )
    ).all()
    definitions, references = [], []
    for file_path, structure in rows:
        defined = False
        for sym in (structure or {}).get("classes", []) + (structure or {}).get("functions", []):
            if sym["name"] == target:
                defined = True
                definitions.append(
                    {
                        "file": file_path,
                        "lines": [sym["start_line"], sym["end_line"]],
                        "kind": sym["kind"],
                        "class": sym.get("parent_class"),
                        "signature": sym.get("signature"),
                        "doc": sym.get("docstring"),
                    }
                )
        if not defined:
            references.append(file_path)
    return {
        "target": target,
        "kind": "symbol",
        "definitions": definitions,
        "mentioned_in": len(references),
        "mentioned_in_files": sorted(references)[:limit],
    }


def changed_files(diff: str) -> list[str]:
    return sorted(set(_DIFF_FILE.findall(diff)))


async def review_context(
    session: AsyncSession, mem: Memory, *, diff: str | None = None, paths: list[str] | None = None
) -> dict:
    """Everything the memory knows about the files a change touches, in one answer."""
    files = [mem.rel(p) for p in (paths or [])] or changed_files(diff or "")
    files = files[:20]
    per_file = []
    for f in files:
        hist = await file_history(session, mem, f, limit=3)
        bugs = past_bugs(mem, path=f, limit=3)
        dependents = await impact(session, mem, f, limit=5)
        per_file.append(
            {
                "path": f,
                "merged_prs": hist["merged_prs"],
                "defects_traced_here": hist["defects_traced_here"],
                "recent_prs": hist["recent_prs"],
                "past_bugs": bugs["bugs"],
                "included_or_imported_by": dependents.get("included_or_imported_by"),
                "some_dependents": dependents.get("dependents"),
            }
        )
    return {
        "files": per_file,
        "lessons": lessons(mem, files)["lessons"],
        "how_to_use": (
            "Check the change against each lesson and past bug in these areas; a file with "
            "many dependents needs its callers checked too. Absence of history is not "
            "evidence of safety."
        ),
    }


def scoreboard(mem: Memory) -> dict:
    """How often merged PRs needed a later fix, and where, from the traced defects."""
    prs = sorted(mem.prs.values(), key=lambda p: p.merged_at)
    if not prs:
        return {"error": "no merged PRs in the history"}
    by_fix: dict[int, list[DefectLink]] = collections.defaultdict(list)
    for link in mem.links:
        by_fix[link.fix_pr].append(link)
    primary: dict[int, DefectLink] = {}
    inherited = 0
    for fix, links in by_fix.items():
        top = max(links, key=lambda link: link.share)
        if top.share < PRIMARY_SHARE:
            continue
        if top.introducing_pr is None:
            inherited += 1
        elif top.introducing_pr in mem.prs:
            primary[fix] = top
    introducing = {link.introducing_pr for link in primary.values()}
    fixes = [p for p in prs if p.kind == "fix"]

    def rate(group: list[HistoryPR]) -> dict:
        n = len(group)
        k = sum(1 for p in group if p.number in introducing)
        return {"merged": n, "later_needed_a_fix": k, "rate": round(k / n, 3) if n else None}

    sizes = {"<50 lines": (0, 50), "50-199": (50, 200), "200-499": (200, 500), "500+": (500, 10**9)}
    areas = collections.Counter()
    for link in primary.values():
        for entry in link.lines:
            areas["/".join(entry["file"].split("/")[:2])] += 1
    days = [
        (mem.prs[fix].merged_at - mem.prs[link.introducing_pr].merged_at).days
        for fix, link in primary.items()
    ]
    months: dict[str, list[HistoryPR]] = collections.defaultdict(list)
    for p in prs:
        months[p.merged_at.strftime("%Y-%m")].append(p)
    return {
        "window": {
            "first": prs[0].merged_at.date().isoformat(),
            "last": prs[-1].merged_at.date().isoformat(),
        },
        "merged_prs": len(prs),
        "fix_prs": len(fixes),
        "fixes_traced_to_a_pr": len(primary),
        "fixes_of_inherited_code": inherited,
        "escape_rate": rate(prs),
        "by_kind": {
            kind: rate([p for p in prs if p.kind == kind]) for kind in sorted({p.kind for p in prs})
        },
        "by_size": {
            label: rate([p for p in prs if lo <= p.additions < hi])
            for label, (lo, hi) in sizes.items()
        },
        "defects_by_area": dict(areas.most_common(10)),
        "days_until_fixed": {
            "median": statistics.median(days) if days else None,
            "p90": sorted(days)[int(0.9 * (len(days) - 1))] if days else None,
        },
        "fixes_that_fixed_a_fix": sum(
            1 for link in primary.values() if mem.prs[link.introducing_pr].kind == "fix"
        ),
        "by_month": {m: rate(group) for m, group in sorted(months.items())},
        "definitions": {
            "later_needed_a_fix": (
                f"a later fix PR changed lines this PR wrote, and this PR holds at least "
                f"{PRIMARY_SHARE:.0%} of the fix's traced lines"
            ),
            "caveats": [
                "Line tracing is evidence, not proof (a refactor that moved buggy code is credited with it).",
                "Only fixes made as PRs with a fix kind are seen.",
                "Recent months look better than they are: their bugs may not have been found yet.",
            ],
        },
    }


def repo_for_directory(directory: Path, repos: list[Repo]) -> Repo | None:
    """The tracked repository whose clone contains ``directory``."""
    d = directory.resolve()
    best = None
    for repo in repos:
        root = Path(repo.root_path)
        inside = d == root or root in d.parents
        if inside and (best is None or len(str(root)) > len(best.root_path)):
            best = repo
    return best
