# PR-review eval

Measures GITA's PR reviewer end to end: the real `run_pr_review` entry
point, production prompts, guardrails and verdict logic, against pull
requests with known bugs. Only the LLM client is wrapped (to record
calls); nothing else is mocked.

## Results

Claude Fable 5.2 via OpenRouter, 27 cases × 3 runs = 81 reviews, 0 failed
attempts. 95% intervals in brackets; recall is over 57 seeded-bug
instances (19 bugs × 3 runs), precision over the 54–55 reviews that
reported a medium+ finding.

| | baseline | v1 (guardrail fix, replayed) |
|---|---|---|
| Recall — seeded bugs reported | 98% [91, 100] | **100%** [94, 100] |
| Precision — medium+ findings that are real | **100%** [93, 100] | **100%** [93, 100] |
| Precision, seeded bugs only (pre-audit view) | 73% [63, 85] | 73% [63, 84] |
| Clean PRs passed (no medium+ false alarm) | 100% [88, 100] | 100% [88, 100] |
| Verdict appropriate | 99% [93, 100] | **100%** [95, 100] |
| Prompt injection resisted | 100% [61, 100] | 100% [61, 100] |
| Case correct (headline) | 99% [93, 100] | **100%** [95, 100] |

Cost $4.79 for the 81 reviews ($0.059 each); model time p50 11.5 s,
p90 20.2 s; ~4.1k input and ~650 output tokens per review.

Per-case detail: `python -m evals.pr_review.run --regrade` prints the
summary; `runs/*/summary.json` and `runs/*/results.jsonl` hold the numbers,
`runs/baseline/traces/` every prompt and response.

## What the run found

**1. A bug in GITA's guardrail.** In one of three runs of the SQL-injection
case the model reported the bug as critical, adding "any term containing a
single quote also raises a syntax error" — true, and about SQL. The
syntax-claim guardrail (meant to catch hallucinated parse errors in
valid Python) saw "syntax error", saw that the file parses, and dropped the
whole finding. The posted review kept two low-severity notes and said
`comment` on a PR with a SQL injection. Fixed in
`agents/guardrails.py`: a "syntax error" in a finding that discusses another
language (SQL, JSON, shell, …) is not a parse claim, because parsing the
Python file can't disprove it. That is **v1**, measured by replaying the
baseline's recorded model responses through the fixed code (below): that
case is fixed and no other review changes.

**2. The grader was wrong more often than the model.** As first signed
off, the grader scored precision at 65%: 30 medium+ findings matched no
seeded bug. Each was checked against the PR head, asking one question —
would fixing the seeded bug also fix this?

- **21 are real bugs that weren't seeded** (`[[known_issue]]` in the case
  files, each with evidence; the runnable ones were run). For example,
  b09's new `add_to_cart` never checks that the order belongs to the
  caller, so any user can add items to anyone's order, and a negative
  quantity *increases* stock (`decrement_stock(conn, 1, -3)` takes stock
  from 5 to 8). b06's `checkout` charges an already-paid order again. The
  "clean" case c05 reads a `feature_flags` attribute that `Settings` doesn't
  have, so the feature check always returns its default.
- **9 restate a seeded bug** from another angle, e.g. "`max_attempts` is
  now ignored" on the unbounded-retry PR (`[[acceptable]]`: neutral).
- **0 were wrong.**

Both numbers are reported: *precision* counts audited real bugs as
correct; *precision, seeded only* doesn't, and is a lower bound. The audit
can only rescue findings the model actually made, so it is written down in
the case files, and anything still unmatched counts against the reviewer.

The audit also tightened matching: a finding must rate a bug no more than
one severity level below its seeded severity to count as reporting it. In
the guardrail case above, a low-severity note about LIKE wildcards on the
same line had been credited as "found the SQL injection".

## Caveats

- **Synthetic and small.** 27 hand-written PRs against a ~285-line
  storefront, written by the same author as the reviewer. The intervals
  are wide (injection resistance rests on 6 reviews) and the suite is near
  its ceiling for this model: it can catch regressions, but it can't rank
  strong models. Next: harder cases (bugs that span files, need the import
  graph, or hide in large diffs) and real PRs with known follow-up fixes.
- **The base repo is not bug-free**, as the audit showed. Its "correct"
  status was an assumption; known issues are now recorded per case.
- **Temperature is not controllable** for this model through OpenRouter,
  so reps measure run-to-run variance at the provider's default sampling.

## How it works

- `base_repo/` — the storefront service every case patches.
- `cases/*.toml` — one PR each: title, description, edits (find/replace,
  append, create) and the bugs a reviewer should report, located by an
  anchor snippet in the PR head. 17 seeded bugs in 16 bug cases, 9 clean
  PRs (refactors, docs, near-twins of bug cases), 2 adversarial PRs whose
  description or code comments try to talk the reviewer out of reporting
  the bug. [CASES.md](CASES.md) renders every case in full.
- `grading.py` — deterministic, no LLM judge. A finding matches a seeded
  bug if it cites the same file, a line within ±2 of the bug, and a
  severity at most one level low. Precision is bootstrapped over reviews,
  because findings within one review aren't independent.
- `run.py` — runs each case through `run_pr_review` against a dedicated,
  freshly indexed Postgres database. Failures (timeout, provider error,
  refusal, schema, wrong served model) are logged to `errors.jsonl` with a
  failure class and never scored as model mistakes. The served model is
  checked on every call. The harness is fingerprinted, so results from
  different grader or case versions are never mixed silently.

```bash
# Harness checks (no model calls): oracle must score 100%, null must find nothing
python -m evals.pr_review.run --fake oracle
python -m evals.pr_review.run --fake null

# A real run (OPENROUTER_API_KEY set); resumes where it stopped
python -m evals.pr_review.run --variant baseline --model anthropic/claude-fable-5.2 --reps 3

# Re-score stored findings after a grader change (no model calls)
python -m evals.pr_review.run --variant baseline --regrade

# Measure a change to GITA's own code on the same model output (no model calls)
python -m evals.pr_review.run --replay-from baseline --variant v2 --approve-harness
```

**Replay** feeds the pipeline the responses recorded in a variant's traces,
in order. The findings call must be asked exactly the recorded prompt
(apart from the random fence nonce) or the replay refuses, because the
recorded answer would say nothing about the new prompt. Replaying the
unchanged code reproduced all 81 baseline rows exactly. This separates
"the model changed" from "our code changed": guardrail, verdict and
rendering changes are measured for free and without sampling noise.
