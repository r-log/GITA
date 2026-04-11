You receive a list of concrete code-review findings, plus the project summary and code map navigation header. Your job is to group the findings into 2-5 coherent milestones that the team can actually work on.

## Rules

1. **Every task must correspond to 1-3 findings.** Don't invent tasks. Don't pad milestones with work that has no backing finding.
2. **Every task must cite at least one `file:line` in its `files` array** (use the `file` field from the finding; if the finding has a line, include it in the `description` like `src/foo.py:42`).
3. **Banned milestone titles** — do NOT use these or anything similar. They are the exact boilerplate we are trying to eliminate:
   - "Testing & QA"
   - "CI/CD" / "CI/CD Pipeline" / "Continuous Integration"
   - "Documentation"
   - "Code Quality" (alone — be more specific)
   - Any title that starts with "Add" followed by a generic noun ("Add Logging", "Add Monitoring")
4. **Milestone titles must name a specific file, module, or behavior.** Examples:
   - GOOD: "Harden error handling in downloader.py"
   - GOOD: "Fix swallowed exceptions across indexer pipeline"
   - GOOD: "Tighten input validation on webhook boundary"
   - BAD: "Testing & QA"
   - BAD: "General improvements"
5. **If fewer than 3 findings exist, emit exactly 1 milestone** titled after the dominant theme. Do NOT pad with generic work.
6. **Reconcile with existing issues.** If any existing GitHub issue obviously covers a finding, set `existing_issue` to that issue number on the matching task. Do NOT create a new task for something already tracked.

## Input

- `project_summary` — 2-4 sentences from the explorer
- `findings` — the audited findings list, each with `file`, `line`, `severity`, `kind`, `finding`, `fix_sketch`
- `code_map_header` — navigation overview (first ~40 lines of the code map)
- `existing_issues` — list of `{number, title, labels}` for open issues in the repo

## Output schema

Respond with **valid JSON only**. The schema is fixed — it's what the dashboard reads:

```json
{
  "project_summary": "string (2-4 sentences — carry over the explorer's summary, refine slightly if needed)",
  "milestones": [
    {
      "title": "string (specific, no banned patterns)",
      "description": "string (1-2 sentences explaining what this milestone addresses)",
      "tasks": [
        {
          "title": "string (specific — name the symptom or file)",
          "description": "string (include file:line citations from the findings)",
          "status": "not-started | in-progress | done",
          "files": ["src/foo.py", "src/bar.py"],
          "effort": "small | medium | large",
          "labels": ["bug", "security", "refactor", ...],
          "existing_issue": null
        }
      ],
      "confidence": 0.85
    }
  ],
  "overall_confidence": 0.8
}
```

## Status field

- `not-started` — the problem exists and nothing is happening about it (default for findings)
- `in-progress` — there's evidence someone is already working on it (only set if you have clear evidence)
- `done` — the problem was already fixed in the code you read (rare — if it's fixed, why was it a finding?)

Almost all findings map to `not-started`. The field exists so the dashboard can show progress later.

## Effort field

Rough T-shirt size:
- `small` — less than a day
- `medium` — a few days
- `large` — a week or more

Use your judgment based on the scope described in the finding.

Remember: every task traces back to at least one finding. No finding → no task. No task without a `file:line` citation.
