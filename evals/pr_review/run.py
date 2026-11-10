"""Run the PR-review eval.

    python -m evals.pr_review.run --model anthropic/claude-sonnet-4.5 --reps 3
    python -m evals.pr_review.run --fake oracle   # harness check: should score ~100%
    python -m evals.pr_review.run --fake null     # harness check: should find nothing

Every case goes through the real entry point, ``run_pr_review``: the base
repo is indexed into a dedicated Postgres database, the PR's files and
head contents are built the way GitHub serves them, and the production
prompts, guardrails and verdict logic run unchanged. Only the LLM client
is wrapped, to record each call's model, usage, latency and transcript.

Output goes to ``evals/pr_review/runs/<variant>/``:

* ``results.jsonl`` — one row per (case, rep), written as it completes.
  Resume is keyed on (case, rep): re-running skips what's there.
* ``traces/<case>_rep<k>.json`` — the full exchange plus the review that
  would have been posted.
* ``errors.jsonl`` — attempts that produced nothing scorable (timeout,
  provider error, truncation, refusal, schema failure, wrong model),
  with a failure class. They never occupy a results row, so resume
  retries them, and they are never scored as model failures.

The harness (runner, grader, cases, base repo) is fingerprinted; a run
refuses to start if it changed since it was last approved with
``--approve-harness``, so numbers from different harness versions are
never mixed silently.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from evals.pr_review.cases import BASE_REPO, CASES_DIR, EVAL_DIR, Case, load_cases
from evals.pr_review.grading import ReportedFinding, aggregate, grade_case
from gita.agents.onboarding.schemas import FindingsResponse, LLMFinding
from gita.agents.pr_reviewer import parse_pr_files, run_pr_review
from gita.agents.pr_reviewer.bridge import build_pr_review_decision
from gita.agents.pr_reviewer.schemas import ReviewSummaryResponse
from gita.config import settings
from gita.github.client import PRInfo
from gita.indexer.ingest import index_repository
from gita.llm.client import (
    FakeLLMClient,
    LLMClient,
    LLMRefusalError,
    LLMResponse,
    LLMSchemaError,
    LLMTransientError,
    LLMTruncatedError,
    OpenRouterClient,
)

RUNS_DIR = EVAL_DIR / "runs"
REPO_NAME = "shop-eval"
REPO_FULL_NAME = "eval/shop"
HARNESS_FILES = ("run.py", "grading.py", "cases.py")


# ---------------------------------------------------------------------------
# Recording wrapper around the real client
# ---------------------------------------------------------------------------
class ServedModelMismatch(RuntimeError):
    pass


def model_matches(requested: str, served: str) -> bool:
    """Accept alias -> snapshot resolution, reject a different model.

    ``anthropic/claude-sonnet-4.5`` may be served as
    ``anthropic/claude-4.5-sonnet-20250929``: same provider, and every
    name token of the request appears in the served id.
    """
    if served == requested or served.startswith(requested):
        return True
    req_provider, _, req_name = requested.partition("/")
    srv_provider, _, srv_name = served.partition("/")
    if req_provider != srv_provider:
        return False
    tokens = lambda s: set(re.findall(r"[a-z]+|\d+", s.lower()))  # noqa: E731
    return tokens(req_name) <= tokens(srv_name)


@dataclass
class RecordedCall:
    system: str
    user: str
    content: str
    model: str
    usage: dict[str, Any]
    latency_s: float
    finish_reason: str | None


@dataclass
class RecordingLLM:
    """Delegates to the real client; keeps what each call cost and said."""

    inner: LLMClient
    requested_model: str | None
    calls: list[RecordedCall] = field(default_factory=list)

    async def call(self, *, system: str, user: str, **kwargs: Any) -> LLMResponse:
        start = time.monotonic()
        response = await self.inner.call(system=system, user=user, **kwargs)
        self.calls.append(
            RecordedCall(
                system=system,
                user=user,
                content=response.content,
                model=response.model,
                usage=dict(response.usage or {}),
                latency_s=time.monotonic() - start,
                finish_reason=response.finish_reason,
            )
        )
        if self.requested_model and not model_matches(self.requested_model, response.model):
            raise ServedModelMismatch(
                f"requested {self.requested_model!r}, served {response.model!r}"
            )
        return response


# ---------------------------------------------------------------------------
# Harness checks: oracle and null "models"
# ---------------------------------------------------------------------------
def fake_llm(case: Case, mode: str) -> FakeLLMClient:
    """Canned responses for one case. ``oracle`` reports exactly the
    expected bugs; ``null`` reports nothing."""
    findings: list[LLMFinding] = []
    if mode == "oracle":
        findings = [
            LLMFinding(
                file=bug.file,
                line=bug.start_line,
                severity=bug.severity,
                kind=bug.kinds[0],
                description=f"oracle: {bug.id}",
            )
            for bug in case.expected
        ]
    responses: list[BaseModel] = [FindingsResponse(findings=findings)]
    responses.append(ReviewSummaryResponse(summary=f"{mode} review", confidence=0.9))
    return FakeLLMClient(responses=responses)


# ---------------------------------------------------------------------------
# Environment: a dedicated, freshly indexed database
# ---------------------------------------------------------------------------
def _eval_db_url() -> str:
    base, _, name = settings.database_url.rpartition("/")
    return f"{base}/{name}_eval"


async def prepare_database() -> async_sessionmaker[AsyncSession]:
    """Create/migrate the eval DB and index a fresh copy of the base repo."""
    from alembic.config import Config

    from alembic import command

    url = _eval_db_url()
    base, _, name = url.rpartition("/")
    admin = create_async_engine(
        f"{base}/postgres", isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    async with admin.connect() as conn:
        exists = await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
        )
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    await admin.dispose()

    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    await asyncio.to_thread(command.upgrade, cfg, "head")

    engine = create_async_engine(url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    with tempfile.TemporaryDirectory(prefix="gita-eval-") as tmp:
        repo_copy = Path(tmp) / "shop"
        shutil.copytree(BASE_REPO, repo_copy)
        async with sessions() as session:
            await session.execute(
                text(
                    "TRUNCATE agent_actions, import_edges, code_index, repos RESTART IDENTITY CASCADE"
                )
            )
            await index_repository(
                session, REPO_NAME, repo_copy, github_full_name=REPO_FULL_NAME, force_full=True
            )
            await session.commit()
    return sessions


# ---------------------------------------------------------------------------
# One case
# ---------------------------------------------------------------------------
def _pr_info(case: Case, files: list[dict]) -> PRInfo:
    return PRInfo(
        # Stable per case (hash() is randomised per process).
        number=1000 + int(hashlib.sha1(case.id.encode()).hexdigest(), 16) % 9000,
        title=case.title,
        body=case.body,
        author="eval-author",
        state="open",
        base_ref="main",
        head_ref=f"eval/{case.id}",
        head_sha=hashlib.sha1(case.id.encode()).hexdigest(),
        changed_files=len(files),
        additions=sum(f["additions"] for f in files),
        deletions=sum(f["deletions"] for f in files),
        html_url="",
    )


def _usage_totals(calls: list[RecordedCall]) -> dict[str, Any]:
    def total(key: str) -> int:
        return sum(int(c.usage.get(key) or 0) for c in calls)

    costs = [c.usage.get("cost") for c in calls]
    return {
        "input_tokens": total("prompt_tokens"),
        "output_tokens": total("completion_tokens"),
        "cost_usd": sum(float(c) for c in costs) if all(c is not None for c in costs) else None,
    }


def _trace(case: Case, calls: list[RecordedCall], review_body: str) -> list[dict]:
    turns: list[dict] = []
    for call in calls:
        turns.append({"role": "system", "content": call.system})
        turns.append({"role": "user", "content": call.user})
        turns.append({"role": "assistant", "content": call.content})
    turns.append(
        {"role": "assistant", "content": f"**Review that would be posted:**\n\n{review_body}"}
    )
    return turns


def _failure_class(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ServedModelMismatch):
        return "served_model_mismatch"
    if isinstance(exc, LLMTruncatedError):
        return "truncated"
    if isinstance(exc, LLMRefusalError):
        return "refusal"
    if isinstance(exc, LLMSchemaError):
        return "schema"
    if isinstance(exc, LLMTransientError):
        return "provider_error"
    return "harness_error"


@dataclass
class Outcome:
    row: dict | None = None
    trace: list[dict] | None = None
    error: dict | None = None


async def run_case(
    case: Case,
    rep: int,
    *,
    sessions: async_sessionmaker[AsyncSession],
    make_llm,
    timeout_s: float,
) -> Outcome:
    files = case.pr_files_json()
    hunks = parse_pr_files(files)
    pr = _pr_info(case, files)
    llm: RecordingLLM = make_llm(case)
    start = time.monotonic()
    try:
        async with sessions() as session:
            result = await asyncio.wait_for(
                run_pr_review(
                    session, REPO_NAME, pr, hunks, llm=llm, head_contents=dict(case.after)
                ),
                timeout=timeout_s,
            )
    except Exception as exc:  # noqa: BLE001 — every failure is recorded, never scored
        return Outcome(
            error={
                "prompt_id": case.id,
                "rep": rep,
                "failure_class": _failure_class(exc),
                "error": repr(exc)[:2000],
                "model": llm.calls[-1].model if llm.calls else None,
                "usage": _usage_totals(llm.calls),
            }
        )
    wall_s = time.monotonic() - start

    findings = [
        ReportedFinding(f.file, f.line, f.severity, f.kind, f.description) for f in result.findings
    ]
    grade = grade_case(case, findings, result.verdict)
    review_body = build_pr_review_decision(
        result, REPO_FULL_NAME, pr.number, head_sha=pr.head_sha
    ).payload["body"]
    usage = _usage_totals(llm.calls)
    models = sorted({c.model for c in llm.calls})
    row = {
        "prompt_id": case.id,
        "rep": rep,
        "prompt": f"PR: {case.title}\n\n{case.body}\n\n```diff\n{case.diff_text()}\n```",
        "tags": [case.kind, *case.tags],
        "status": "ok",
        "stop_reason": ",".join(str(c.finish_reason) for c in llm.calls),
        "grade": grade.metrics,
        "explanation": {"correct": grade.explanation},
        "model": models[0] if len(models) == 1 else ",".join(models),
        "usage": {"input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"]},
        "in_tokens": usage["input_tokens"],
        "out_tokens": usage["output_tokens"],
        "cost_usd": usage["cost_usd"],
        "latency_s": round(sum(c.latency_s for c in llm.calls), 3),
        "wall_s": round(wall_s, 3),
        "meta": {
            "verdict": result.verdict,
            "findings": len(result.findings),
            "dropped_by_guardrails": result.dropped_findings,
            "unreviewed_files": result.unreviewed_files,
            "llm_calls": len(llm.calls),
            "notes": case.notes,
            "grading": grade.record(),
            # Verified findings, kept so --regrade can re-score without model calls.
            "findings_detail": [
                {
                    "file": f.file,
                    "line": f.line,
                    "severity": f.severity,
                    "kind": f.kind,
                    "description": f.description,
                    "fix_sketch": f.fix_sketch,
                }
                for f in result.findings
            ],
        },
    }
    return Outcome(row=row, trace=_trace(case, llm.calls, review_body))


# ---------------------------------------------------------------------------
# Harness fingerprint
# ---------------------------------------------------------------------------
def harness_sha() -> str:
    digest = hashlib.sha256()
    paths = [EVAL_DIR / name for name in HARNESS_FILES]
    paths += sorted(CASES_DIR.glob("*.toml"))
    paths += sorted(p for p in BASE_REPO.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    for path in paths:
        digest.update(str(path.relative_to(EVAL_DIR)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _append_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def regrade(variant_dir: Path, cases: list[Case]) -> None:
    """Re-score stored results with the current grader; no model calls."""
    by_id = {c.id: c for c in cases}
    path = variant_dir / "results.jsonl"
    rows = _read_jsonl(path)
    for row in rows:
        detail = row["meta"].get("findings_detail")
        case = by_id.get(row["prompt_id"])
        if detail is None or case is None:
            raise SystemExit(
                f"cannot regrade {row['prompt_id']} rep{row['rep']}: no stored findings"
            )
        findings = [
            ReportedFinding(d["file"], d["line"], d["severity"], d["kind"], d["description"])
            for d in detail
        ]
        grade = grade_case(case, findings, row["meta"]["verdict"])
        row["grade"] = grade.metrics
        row["explanation"] = {"correct": grade.explanation}
        row["meta"]["grading"] = grade.record()
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    tmp.replace(path)
    print(f"regraded {len(rows)} rows in {variant_dir}")


def summarize(variant_dir: Path) -> dict:
    """Aggregate a variant's results, write summary.json and print it."""
    rows = [r for r in _read_jsonl(variant_dir / "results.jsonl") if r.get("status") == "ok"]
    agg = aggregate(r["meta"]["grading"] for r in rows)
    costs = [r["cost_usd"] for r in rows if r.get("cost_usd") is not None]

    def mean(key: str) -> float | None:
        return round(sum(r[key] for r in rows) / len(rows), 2) if rows else None

    summary = {
        "graded": len(rows),
        "errors": len(_read_jsonl(variant_dir / "errors.jsonl")),
        "metrics": {
            k: {"value": v.value, "ci_lo": v.lo, "ci_hi": v.hi, "n": v.n} for k, v in agg.items()
        },
        "cost_usd_total": round(sum(costs), 4) if len(costs) == len(rows) and rows else None,
        "mean_latency_s": mean("latency_s"),
        "mean_in_tokens": mean("in_tokens"),
        "mean_out_tokens": mean("out_tokens"),
        "models": sorted({r["model"] for r in rows}),
    }
    (variant_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    for name, est in agg.items():
        print(f"  {name:20} {est.fmt()}")
    print(
        f"  graded: {summary['graded']}  errors: {summary['errors']}  "
        f"cost: {summary['cost_usd_total']}  mean latency: {summary['mean_latency_s']}s  "
        f"models: {summary['models']}"
    )
    return summary


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--variant", default="baseline", help="baseline or v<N>")
    parser.add_argument(
        "--model", default=None, help="OpenRouter model id (default: AI_DEFAULT_MODEL)"
    )
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--cases", default="", help="comma-separated case ids (default: all)")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout-s", type=float, default=300.0)
    parser.add_argument("--fake", choices=("oracle", "null"), help="harness check, no LLM calls")
    parser.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    parser.add_argument("--approve-harness", action="store_true")
    parser.add_argument(
        "--regrade", action="store_true", help="re-score stored results only (no model calls)"
    )
    args = parser.parse_args(argv)

    cases = load_cases(only={c for c in args.cases.split(",") if c} or None)
    if args.regrade:
        variant_dir = args.runs_dir / (f"_checks/{args.fake}" if args.fake else args.variant)
        regrade(variant_dir, load_cases())
        summarize(variant_dir)
        return 0
    args.runs_dir.mkdir(parents=True, exist_ok=True)
    state_path = args.runs_dir / "_state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    sha = harness_sha()
    if args.fake is None:
        if args.approve_harness:
            state["harness_sha"] = sha
            state_path.write_text(json.dumps(state, indent=2) + "\n")
        elif state.get("harness_sha") != sha:
            print(
                "harness changed since it was last approved (runner, grader, cases or base repo).\n"
                "Review the change, then re-run with --approve-harness.",
                file=sys.stderr,
            )
            return 2

    variant = f"_checks/{args.fake}" if args.fake else args.variant
    if not args.fake and not re.fullmatch(r"baseline|v\d+", variant):
        parser.error("--variant must be 'baseline' or 'v<N>'")
    variant_dir = args.runs_dir / variant
    (variant_dir / "traces").mkdir(parents=True, exist_ok=True)
    done = {(r["prompt_id"], r["rep"]) for r in _read_jsonl(variant_dir / "results.jsonl")}

    model = args.model or settings.ai_default_model
    client: OpenRouterClient | None = None
    if args.fake:

        def make_llm(case: Case) -> RecordingLLM:
            return RecordingLLM(fake_llm(case, args.fake), requested_model=None)
    else:
        key = settings.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY")
        # With a key held by the network proxy, any placeholder works here.
        client = OpenRouterClient(
            api_key=key or "placeholder-key-injected-by-proxy", default_model=model
        )

        def make_llm(case: Case) -> RecordingLLM:
            return RecordingLLM(client, requested_model=model)

    sessions = await prepare_database()
    todo = [(c, rep) for rep in range(args.reps) for c in cases if (c.id, rep) not in done]
    print(
        f"{variant}: {len(todo)} to run ({len(done)} already done), model={'fake:' + args.fake if args.fake else model}"
    )
    semaphore = asyncio.Semaphore(args.concurrency)
    start = time.monotonic()

    async def worker(case: Case, rep: int) -> None:
        async with semaphore:
            outcome = await run_case(
                case, rep, sessions=sessions, make_llm=make_llm, timeout_s=args.timeout_s
            )
        if outcome.error is not None:
            _append_jsonl(variant_dir / "errors.jsonl", outcome.error)
            print(f"  ERROR {case.id} rep{rep}: {outcome.error['failure_class']}")
            return
        (variant_dir / "traces" / f"{case.id}_rep{rep}.json").write_text(
            json.dumps(outcome.trace, indent=2) + "\n"
        )
        _append_jsonl(variant_dir / "results.jsonl", outcome.row)
        print(
            f"  {case.id} rep{rep}: correct={outcome.row['grade']['correct']:.0f} "
            f"verdict={outcome.row['meta']['verdict']}"
        )

    try:
        await asyncio.gather(*(worker(c, rep) for c, rep in todo))
    finally:
        if client is not None:
            await client.aclose()
    print(f"done in {time.monotonic() - start:.1f}s")
    summarize(variant_dir)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
