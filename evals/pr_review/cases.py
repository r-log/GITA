"""Load PR-review eval cases.

A case is a TOML file describing a pull request against ``base_repo/``:
the PR title and description, a list of edits that produce the PR head,
and the bugs a good reviewer should report. Edits are applied here, the
diff is built the way GitHub's ``pulls/{n}/files`` endpoint shapes it,
and each expected bug is resolved to a line range in the PR head by
locating an anchor snippet — so nothing in the code under review hints
at the answer.

Edit operations (exactly one per edit):

* ``find`` + ``replace`` — ``find`` must occur exactly once in the file.
* ``append`` — text appended to the end of the file.
* ``create`` — a new file with this content.

``[[acceptable]]`` entries describe extra findings a good reviewer may
legitimately add (for instance, reporting a prompt-injection attempt as a
security issue). A finding that matches one is neither rewarded nor
penalised: ``description_regex`` must match its description, and
``kinds`` / ``file``, when given, must match too.
"""

from __future__ import annotations

import difflib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
BASE_REPO = EVAL_DIR / "base_repo"
CASES_DIR = EVAL_DIR / "cases"

KINDS = ("bug", "clean", "adversarial")
SEVERITIES = ("low", "medium", "high", "critical")


class CaseError(ValueError):
    """A case file is malformed or doesn't apply to the base repo."""


@dataclass(frozen=True)
class ExpectedBug:
    id: str
    file: str
    start_line: int  # PR-head line numbers, inclusive
    end_line: int
    kinds: tuple[str, ...]
    severity: str


@dataclass(frozen=True)
class AcceptableExtra:
    id: str
    description_regex: re.Pattern[str]
    kinds: tuple[str, ...] = ()
    file: str | None = None


@dataclass
class Case:
    id: str
    kind: str
    tags: list[str]
    title: str
    body: str
    notes: str
    before: dict[str, str]  # path -> content at base (changed files only)
    after: dict[str, str]  # path -> content at PR head (changed files only)
    expected: list[ExpectedBug] = field(default_factory=list)
    acceptable: list[AcceptableExtra] = field(default_factory=list)

    @property
    def is_positive(self) -> bool:
        return self.kind in ("bug", "adversarial")

    def pr_files_json(self) -> list[dict]:
        """The PR's files in the shape of GitHub's ``pulls/{n}/files``."""
        files = []
        for path in sorted(self.after):
            old = self.before.get(path)
            new = self.after[path]
            old_lines = (old or "").splitlines()
            new_lines = new.splitlines()
            diff = list(difflib.unified_diff(old_lines, new_lines, n=3, lineterm=""))
            patch = "\n".join(diff[2:])  # drop the ---/+++ headers, as GitHub does
            additions = sum(1 for d in diff[2:] if d.startswith("+"))
            deletions = sum(1 for d in diff[2:] if d.startswith("-"))
            files.append(
                {
                    "filename": path,
                    "status": "added" if old is None else "modified",
                    "additions": additions,
                    "deletions": deletions,
                    "patch": patch,
                }
            )
        return files

    def diff_text(self) -> str:
        """Unified diff with file headers, for humans reviewing the case."""
        chunks = []
        for f in self.pr_files_json():
            chunks.append(f"--- a/{f['filename']}\n+++ b/{f['filename']}\n{f['patch']}")
        return "\n".join(chunks)


def _apply_edit(content: str | None, edit: dict, where: str) -> str:
    ops = [k for k in ("find", "append", "create") if k in edit]
    if len(ops) != 1:
        raise CaseError(f"{where}: an edit needs exactly one of find/append/create")
    op = ops[0]
    if op == "create":
        if content is not None:
            raise CaseError(f"{where}: create on an existing file")
        return edit["create"]
    if content is None:
        raise CaseError(f"{where}: {op} on a file that doesn't exist in base_repo")
    if op == "append":
        return content.rstrip("\n") + edit["append"]
    count = content.count(edit["find"])
    if count != 1:
        raise CaseError(f"{where}: find text occurs {count} times (need exactly 1)")
    if "replace" not in edit:
        raise CaseError(f"{where}: find without replace")
    return content.replace(edit["find"], edit["replace"])


def _resolve_expectation(raw: dict, after: dict[str, str], where: str) -> ExpectedBug:
    for key in ("id", "file", "anchor", "kinds", "severity"):
        if key not in raw:
            raise CaseError(f"{where}: expect is missing {key!r}")
    content = after.get(raw["file"])
    if content is None:
        raise CaseError(f"{where}: expect cites {raw['file']!r}, which the PR doesn't change")
    count = content.count(raw["anchor"])
    if count != 1:
        raise CaseError(f"{where}: anchor occurs {count} times in {raw['file']} (need 1)")
    if raw["severity"] not in SEVERITIES:
        raise CaseError(f"{where}: unknown severity {raw['severity']!r}")
    start = content[: content.index(raw["anchor"])].count("\n") + 1
    return ExpectedBug(
        id=raw["id"],
        file=raw["file"],
        start_line=start,
        end_line=start + int(raw.get("span", 0)),
        kinds=tuple(raw["kinds"]),
        severity=raw["severity"],
    )


def load_case(path: Path, base_repo: Path = BASE_REPO) -> Case:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    case_id = path.stem
    kind = data.get("kind")
    if kind not in KINDS:
        raise CaseError(f"{case_id}: kind must be one of {KINDS}")

    before: dict[str, str] = {}
    after: dict[str, str] = {}
    for i, edit in enumerate(data.get("edits", [])):
        rel = edit["file"]
        where = f"{case_id} edit {i} ({rel})"
        if rel not in after:
            base_file = base_repo / rel
            if base_file.is_file():
                before[rel] = base_file.read_text(encoding="utf-8")
                after[rel] = before[rel]
        after[rel] = _apply_edit(after.get(rel), edit, where)
    if not after:
        raise CaseError(f"{case_id}: no edits")

    for rel, content in after.items():
        if content == before.get(rel):
            raise CaseError(f"{case_id}: edits leave {rel} unchanged")
        if rel.endswith(".py"):
            try:
                compile(content, rel, "exec")
            except SyntaxError as exc:
                raise CaseError(f"{case_id}: PR head of {rel} doesn't compile: {exc}") from exc

    expected = [
        _resolve_expectation(raw, after, f"{case_id} expect {i}")
        for i, raw in enumerate(data.get("expect", []))
    ]
    acceptable = []
    for i, raw in enumerate(data.get("acceptable", [])):
        try:
            pattern = re.compile(raw["description_regex"])
        except (KeyError, re.error) as exc:
            raise CaseError(f"{case_id} acceptable {i}: bad description_regex: {exc}") from exc
        acceptable.append(
            AcceptableExtra(
                id=raw.get("id", f"acceptable_{i}"),
                description_regex=pattern,
                kinds=tuple(raw.get("kinds", ())),
                file=raw.get("file"),
            )
        )

    if (kind == "clean") == bool(expected):
        raise CaseError(f"{case_id}: clean cases have no expected bugs; others need at least one")

    return Case(
        id=case_id,
        kind=kind,
        tags=list(data.get("tags", [])),
        title=data["title"],
        body=data.get("body", ""),
        notes=data.get("notes", ""),
        before=before,
        after=after,
        expected=expected,
        acceptable=acceptable,
    )


def load_cases(cases_dir: Path = CASES_DIR, only: set[str] | None = None) -> list[Case]:
    cases = [load_case(p) for p in sorted(cases_dir.glob("*.toml"))]
    if only:
        unknown = only - {c.id for c in cases}
        if unknown:
            raise CaseError(f"unknown case ids: {sorted(unknown)}")
        cases = [c for c in cases if c.id in only]
    return cases
