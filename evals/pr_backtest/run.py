"""Backtest the PR reviewer on real pull requests that a later PR had to fix.

    python -m evals.pr_backtest.run --clone ~/mangos --fake null            # sizes, no model calls
    python -m evals.pr_backtest.run --clone ~/mangos --fake oracle          # grader check
    python -m evals.pr_backtest.run --clone ~/mangos --variant baseline --model anthropic/claude-fable-5.2 --reps 2
    python -m evals.pr_backtest.run --clone ~/mangos --variant v1 ...      # same, without the index

Cases come from ``mine.py`` (``cases.json``): a merged PR, and the lines a
later fix PR changed, located in that PR's head. Each review runs through
the real ``run_pr_review`` exactly as a webhook would trigger it at the
time: the diff and head contents come from git (the shape of GitHub's
``pulls/{n}/files``), and the repository is indexed at the PR's base
commit. Only the PR's title is known (from the merge commit); its
description isn't used.

Variants:

* ``baseline`` — GITA as shipped: the C/C++ index at the PR's base
  (symbols around each change, the files that include a changed header).
* ``v1`` — the same review with an empty index: the diff and windows of
  each changed file only, which is what GITA could do before it indexed C++.

Grading is in ``grading`` below: a finding *locates* a defect when it cites
a file the fix later changed, within ``TOLERANCE`` lines of a changed line.
Whether a locating finding actually describes the defect is checked by
hand and recorded in ``adjudications.toml``; the summary reports both.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from evals.pr_review.run import (
    Outcome,
    RecordingLLM,
    _append_jsonl,
    _failure_class,
    _read_jsonl,
    _usage_totals,
    migrated_database,
)
from gita.agents.onboarding.schemas import FindingsResponse, LLMFinding
from gita.agents.pr_reviewer import parse_pr_files, run_pr_review
from gita.agents.pr_reviewer.bridge import build_pr_review_decision
from gita.agents.pr_reviewer.schemas import ReviewSummaryResponse
from gita.config import settings
from gita.db.models import Repo
from gita.github.client import PRInfo
from gita.indexer.ingest import index_repository
from gita.llm.client import FakeLLMClient, OpenRouterClient

EVAL_DIR = Path(__file__).resolve().parent
RUNS_DIR = EVAL_DIR / "runs"
TOLERANCE = 3
VARIANTS = {"baseline": "indexed", "v1": "diff-only"}


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------
@dataclass
class Bug:
    id: str
    fix_pr: int
    fix_title: str
    visible: str  # "code" | "repo" | "domain"
    origin: str
    note: str
    truth: dict[str, set[int]]  # path -> lines in the PR's head the fix changed


@dataclass
class BacktestCase:
    id: str
    pr: int
    title: str
    base: str
    head: str
    bugs: list[Bug] = field(default_factory=list)


def load_cases(only: set[str] | None = None) -> list[BacktestCase]:
    """Join mined cases with their labels; unlabelled defects are an error."""
    mined = json.loads((EVAL_DIR / "cases.json").read_text())["cases"]
    labels = {
        (lab["case"], lab["bug"]): lab
        for lab in tomllib.loads((EVAL_DIR / "labels.toml").read_text())["label"]
    }
    cases = []
    for raw in mined:
        case = BacktestCase(raw["id"], raw["pr"], raw["title"], raw["base"], raw["head"])
        for bug in raw["bugs"]:
            label = labels.get((raw["id"], bug["id"]))
            if label is None:
                raise SystemExit(f"{raw['id']}/{bug['id']} has no label: label it before running")
            if label["visible"] == "none":
                continue
            case.bugs.append(
                Bug(
                    id=bug["id"],
                    fix_pr=bug["fix_pr"],
                    fix_title=bug["fix_title"],
                    visible=label["visible"],
                    origin=label["origin"],
                    note=label["note"],
                    truth={t["file"]: set(t["lines"]) for t in bug["truth"]},
                )
            )
        if case.bugs:
            cases.append(case)
    if only:
        unknown = only - {c.id for c in cases}
        if unknown:
            raise SystemExit(f"unknown case ids: {sorted(unknown)}")
        cases = [c for c in cases if c.id in only]
    return cases


# ---------------------------------------------------------------------------
# The PR, from git
# ---------------------------------------------------------------------------
def _git(clone: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(clone), *args], capture_output=True, text=True, check=True
    ).stdout


def pr_files(clone: Path, base: str, head: str) -> tuple[list[dict], dict[str, str]]:
    """``pulls/{n}/files``-shaped entries and the head content of each file."""
    files, contents = [], {}
    status_by_code = {"A": "added", "M": "modified", "D": "removed"}
    for line in _git(clone, "diff", "--name-status", "--no-renames", base, head).splitlines():
        code, path = line.split("\t", 1)
        patch = _git(clone, "diff", "-U3", "--no-color", base, head, "--", path)
        body = patch[patch.find("\n@@") + 1 :] if "\n@@" in patch else ""
        if "Binary files" in patch:
            body = ""
        files.append(
            {
                "filename": path,
                "status": status_by_code.get(code[0], "modified"),
                "additions": sum(1 for x in body.splitlines() if x.startswith("+")),
                "deletions": sum(1 for x in body.splitlines() if x.startswith("-")),
                "patch": body.rstrip("\n"),
            }
        )
        if code[0] != "D" and body:
            contents[path] = _git(clone, "show", f"{head}:{path}")
    return files, contents


def added_lines(files: list[dict]) -> dict[str, set[int]]:
    """Head-side line numbers each patch adds (for the chance baseline)."""
    out: dict[str, set[int]] = {}
    for f in files:
        line = 0
        for row in f["patch"].splitlines():
            if row.startswith("@@"):
                line = int(row.split("+", 1)[1].split(",", 1)[0].split(" ", 1)[0]) - 1
            elif row.startswith("+"):
                line += 1
                out.setdefault(f["filename"], set()).add(line)
            elif not row.startswith("-"):
                line += 1
    return out


# ---------------------------------------------------------------------------
# Index: at the PR's base, or empty
# ---------------------------------------------------------------------------
def repo_name(case: BacktestCase, variant: str) -> str:
    return f"backtest-{case.id}-{VARIANTS[variant]}"


async def ensure_indexed(sessions, clone: Path, case: BacktestCase, variant: str) -> None:
    name = repo_name(case, variant)
    async with sessions() as session:
        if (await session.execute(select(Repo).where(Repo.name == name))).scalar_one_or_none():
            return
    with tempfile.TemporaryDirectory(prefix="gita-backtest-") as tmp:
        tree = Path(tmp) / "tree"
        if VARIANTS[variant] == "indexed":
            _git(clone, "worktree", "add", "--detach", "--force", str(tree), case.base)
        else:  # an empty repository: no symbols, no include graph
            tree.mkdir()
            subprocess.run(["git", "init", "-q", str(tree)], check=True)
            subprocess.run(
                ["git", "-C", str(tree), "commit", "-q", "--allow-empty", "-m", "empty"],
                check=True,
                env={
                    "GIT_AUTHOR_NAME": "e",
                    "GIT_AUTHOR_EMAIL": "e@e",
                    "GIT_COMMITTER_NAME": "e",
                    "GIT_COMMITTER_EMAIL": "e@e",
                    "PATH": "/usr/bin:/bin",
                },
            )
        try:
            async with sessions() as session:
                await index_repository(session, name, tree, force_full=True)
                await session.commit()
        finally:
            if VARIANTS[variant] == "indexed":
                _git(clone, "worktree", "remove", "--force", str(tree))


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------
def _near(line: int, lines: set[int]) -> bool:
    return any(abs(line - t) <= TOLERANCE for t in lines)


def grade(case: BacktestCase, findings: list[dict], files: list[dict], reviewed: set[str]) -> dict:
    """Which defects some finding locates, and what chance would give."""
    added = added_lines(files)
    total_added = sum(len(v) for v in added.values()) or 1
    per_bug = []
    for bug in case.bugs:
        hits = [
            i
            for i, f in enumerate(findings)
            if f["file"] in bug.truth and _near(f["line"], bug.truth[f["file"]])
        ]
        # A finding dropped at a random added line lands near this defect with
        # probability p; with k findings, chance locates it with 1-(1-p)^k.
        near = sum(
            1
            for path, lines in added.items()
            for n in lines
            if path in bug.truth and _near(n, bug.truth[path])
        )
        p = near / total_added
        per_bug.append(
            {
                "bug": bug.id,
                "visible": bug.visible,
                "located": bool(hits),
                "hits": hits,
                "in_review": any(path in reviewed for path in bug.truth),
                "chance": round(1 - (1 - p) ** len(findings), 4),
            }
        )
    code = [b for b in per_bug if b["visible"] == "code"]
    return {
        "per_bug": per_bug,
        "metrics": {
            "found": sum(b["located"] for b in per_bug) / len(per_bug),
            "chance": sum(b["chance"] for b in per_bug) / len(per_bug),
            "findings": float(len(findings)),
            **({"found_code": sum(b["located"] for b in code) / len(code)} if code else {}),
        },
    }


# ---------------------------------------------------------------------------
# One review
# ---------------------------------------------------------------------------
def fake_llm(case: BacktestCase, mode: str) -> FakeLLMClient:
    findings = []
    if mode == "oracle":
        for bug in case.bugs:
            path, lines = next(iter(sorted(bug.truth.items())))
            findings.append(
                LLMFinding(
                    file=path, line=min(lines), severity="high", kind="bug", description="oracle"
                )
            )
    return FakeLLMClient(
        responses=[
            FindingsResponse(findings=findings),
            ReviewSummaryResponse(summary=f"{mode} review", confidence=0.9),
        ]
    )


async def run_case(
    case: BacktestCase,
    rep: int,
    variant: str,
    *,
    clone: Path,
    sessions,
    llm: RecordingLLM,
    prepared: dict,
    timeout_s: float,
) -> Outcome:
    files, contents = prepared[case.id]
    pr = PRInfo(
        number=case.pr,
        title=case.title,
        body="",
        author="mangos-contributor",
        state="open",
        base_ref="main",
        head_ref=f"pr/{case.pr}",
        head_sha=case.head,
        changed_files=len(files),
        additions=sum(f["additions"] for f in files),
        deletions=sum(f["deletions"] for f in files),
        html_url="",
    )
    start = time.monotonic()
    try:
        async with sessions() as session:
            result = await asyncio.wait_for(
                run_pr_review(
                    session,
                    repo_name(case, variant),
                    pr,
                    parse_pr_files(files),
                    llm=llm,
                    head_contents=contents,
                ),
                timeout=timeout_s,
            )
    except Exception as exc:  # noqa: BLE001 — recorded, never scored
        return Outcome(
            error={
                "prompt_id": case.id,
                "rep": rep,
                "failure_class": _failure_class(exc),
                "error": repr(exc)[:2000],
                "usage": _usage_totals(llm.calls),
            }
        )
    findings = [
        {
            "file": f.file,
            "line": f.line,
            "severity": f.severity,
            "kind": f.kind,
            "description": f.description,
            "fix_sketch": f.fix_sketch,
        }
        for f in result.findings
    ]
    reviewed = {f["filename"] for f in files} - set(result.unreviewed_files)
    graded = grade(case, findings, files, reviewed)
    usage = _usage_totals(llm.calls)
    models = sorted({c.model for c in llm.calls})
    body = build_pr_review_decision(result, "eval/mangos", case.pr, head_sha=case.head).payload[
        "body"
    ]
    row = {
        "prompt_id": case.id,
        "rep": rep,
        "prompt": f"PR #{case.pr}: {case.title}",
        "tags": [VARIANTS[variant], *sorted({b.visible for b in case.bugs})],
        "status": "ok",
        "stop_reason": ",".join(str(c.finish_reason) for c in llm.calls),
        "grade": graded["metrics"],
        "explanation": {"found": _explain(graded["per_bug"])},
        "model": models[0] if len(models) == 1 else ",".join(models),
        "usage": {"input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"]},
        "in_tokens": usage["input_tokens"],
        "out_tokens": usage["output_tokens"],
        "cost_usd": usage["cost_usd"],
        "latency_s": round(sum(c.latency_s for c in llm.calls), 3),
        "wall_s": round(time.monotonic() - start, 3),
        "meta": {
            "variant": VARIANTS[variant],
            "verdict": result.verdict,
            "dropped_by_guardrails": result.dropped_findings,
            "unreviewed_files": result.unreviewed_files,
            "prompt_chars": sum(len(c.system) + len(c.user) for c in llm.calls[:1]),
            # Per call, as the provider reported it (reasoning tokens included).
            "usage_calls": [c.usage for c in llm.calls],
            "per_bug": graded["per_bug"],
            "findings_detail": findings,
        },
    }
    trace = [
        {"role": role, "content": text}
        for c in llm.calls
        for role, text in (("system", c.system), ("user", c.user), ("assistant", c.content))
    ] + [{"role": "assistant", "content": f"**Review that would be posted:**\n\n{body}"}]
    return Outcome(row=row, trace=trace)


def regrade(out: Path, clone: Path) -> None:
    """Re-score stored reviews against the current cases and labels; no model calls."""
    path = out / "results.jsonl"
    rows = _read_jsonl(path)
    by_id = {c.id: c for c in load_cases()}
    files_by_case: dict[str, list[dict]] = {}
    for row in rows:
        case = by_id.get(row["prompt_id"])
        if case is None:
            raise SystemExit(f"{row['prompt_id']} is no longer a case; move its rows aside first")
        if case.id not in files_by_case:
            files_by_case[case.id] = pr_files(clone, case.base, case.head)[0]
        files = files_by_case[case.id]
        reviewed = {f["filename"] for f in files} - set(row["meta"]["unreviewed_files"])
        graded = grade(case, row["meta"]["findings_detail"], files, reviewed)
        row["grade"] = graded["metrics"]
        row["meta"]["per_bug"] = graded["per_bug"]
        row["explanation"] = {"found": _explain(graded["per_bug"])}
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    print(f"regraded {len(rows)} rows in {out}")


def _explain(per_bug: list[dict]) -> str:
    return "\n".join(
        f"{'LOCATED' if b['located'] else 'missed '} {b['bug']} ({b['visible']})"
        + (f" by findings {b['hits']}" if b["hits"] else "")
        + ("" if b["in_review"] else " [defect's file not reviewed]")
        for b in per_bug
    )


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def _adjudications() -> dict[tuple, bool]:
    path = EVAL_DIR / "adjudications.toml"
    if not path.exists():
        return {}
    return {
        (a["variant"], a["case"], a["rep"], a["bug"], a["finding"]): a["describes"]
        for a in tomllib.loads(path.read_text()).get("adjudication", [])
    }


def summarize(variant_dir: Path, variant: str) -> dict:
    rows = [r for r in _read_jsonl(variant_dir / "results.jsonl") if r.get("status") == "ok"]
    adjudicated = _adjudications()
    tally: dict[str, list[float]] = {
        k: [] for k in ("all", "code", "repo", "domain", "chance_all", "chance_code")
    }
    caught = {"all": [0, 0, 0], "code": [0, 0, 0]}  # caught, pending, total
    for r in rows:
        for b in r["meta"]["per_bug"]:
            for group in ("all", b["visible"]):
                if group in tally:
                    tally[group].append(float(b["located"]))
            tally["chance_all"].append(b["chance"])
            if b["visible"] == "code":
                tally["chance_code"].append(b["chance"])
            verdicts = [
                adjudicated.get((VARIANTS[variant], r["prompt_id"], r["rep"], b["bug"], i))
                for i in b["hits"]
            ]
            for group in ("all", "code"):
                if group == "code" and b["visible"] != "code":
                    continue
                caught[group][2] += 1
                if any(v is True for v in verdicts):
                    caught[group][0] += 1
                elif any(v is None for v in verdicts):
                    caught[group][1] += 1

    def mean(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 3) if xs else None

    costs = [r["cost_usd"] for r in rows if r.get("cost_usd") is not None]
    failed = _read_jsonl(variant_dir / "errors.jsonl")
    summary = {
        "variant": VARIANTS[variant],
        "reviews": len(rows),
        "errors": len(_read_jsonl(variant_dir / "errors.jsonl")),
        "located": {k: mean(v) for k, v in tally.items()},
        "located_n": {k: len(v) for k, v in tally.items() if not k.startswith("chance")},
        "caught_adjudicated": {
            g: {"caught": c, "pending": p, "of": n} for g, (c, p, n) in caught.items()
        },
        "mean_findings": mean([r["grade"]["findings"] for r in rows]),
        "request_changes": mean([float(r["meta"]["verdict"] == "request_changes") for r in rows]),
        "cost_usd_total": round(sum(costs), 4) if costs else None,
        # Billed calls whose output couldn't be used (truncated, refused).
        "failed_cost_usd": round(sum(e["usage"].get("cost_usd") or 0 for e in failed), 4),
        "mean_prompt_chars": mean([r["meta"]["prompt_chars"] for r in rows]),
        "models": sorted({r["model"] for r in rows}),
    }
    (variant_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--clone", type=Path, required=True, help="full clone of the repository")
    parser.add_argument("--variant", default="baseline", choices=sorted(VARIANTS))
    parser.add_argument("--model", default=None)
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--cases", default="")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--timeout-s", type=float, default=600.0)
    parser.add_argument("--fake", choices=("null", "oracle"))
    parser.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    parser.add_argument("--summarize", action="store_true", help="only re-summarize stored rows")
    parser.add_argument(
        "--regrade", action="store_true", help="re-score stored rows (no model calls)"
    )
    args = parser.parse_args(argv)

    out = args.runs_dir / (f"_checks/{args.fake}-{args.variant}" if args.fake else args.variant)
    if args.regrade:
        regrade(out, args.clone.resolve())
    if args.summarize or args.regrade:
        summarize(out, args.variant)
        return 0
    cases = load_cases({c for c in args.cases.split(",") if c} or None)
    (out / "traces").mkdir(parents=True, exist_ok=True)
    done = {(r["prompt_id"], r["rep"]) for r in _read_jsonl(out / "results.jsonl")}
    todo = [(c, r) for r in range(args.reps) for c in cases if (c.id, r) not in done]

    sessions = await migrated_database("backtest")
    clone = args.clone.resolve()
    prepared = {}
    for case in cases:
        await ensure_indexed(sessions, clone, case, args.variant)
        prepared[case.id] = pr_files(clone, case.base, case.head)

    model = args.model or settings.ai_default_model
    client = None
    if not args.fake:
        client = OpenRouterClient(
            api_key=settings.openrouter_api_key or "placeholder-key-injected-by-proxy",
            default_model=model,
        )
    print(f"{out.name}: {len(todo)} reviews to run ({len(done)} done), model={args.fake or model}")
    semaphore = asyncio.Semaphore(args.concurrency)

    async def worker(case: BacktestCase, rep: int) -> None:
        llm = RecordingLLM(
            fake_llm(case, args.fake) if args.fake else client,
            requested_model=None if args.fake else model,
        )
        async with semaphore:
            outcome = await run_case(
                case,
                rep,
                args.variant,
                clone=clone,
                sessions=sessions,
                llm=llm,
                prepared=prepared,
                timeout_s=args.timeout_s,
            )
        if outcome.error is not None:
            _append_jsonl(out / "errors.jsonl", outcome.error)
            print(f"  ERROR {case.id} rep{rep}: {outcome.error['failure_class']}")
            return
        (out / "traces" / f"{case.id}_rep{rep}.json").write_text(
            json.dumps(outcome.trace, indent=1)
        )
        _append_jsonl(out / "results.jsonl", outcome.row)
        print(f"  {case.id} rep{rep}: {outcome.row['explanation']['found'].replace(chr(10), '; ')}")

    try:
        await asyncio.gather(*(worker(c, r) for c, r in todo))
    finally:
        if client is not None:
            await client.aclose()
    summarize(out, args.variant)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
