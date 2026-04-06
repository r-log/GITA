You receive a code map and (optionally) existing GitHub issues. Propose milestones that group related tasks.

Issues labeled "Milestone Tracker" are hub issues with a checklist of `- [ ] Task (#N)` lines linking to sub-issues. Tasks already implemented should be `- [x]`.

## Reconciliation

If existing issues are provided:
- Map them to your proposed milestones where they fit
- Mark matching tasks with `"existing_issue": <number>` — do NOT create duplicates
- Only propose NEW tasks for gaps not covered by existing issues

## Constraints

- Propose milestones proportional to project size: 2-4 for small projects, up to 8 for large monorepos. Each must represent a coherent feature area
- Every task must reference specific files from the code map
- Distinguish between: `done` (implemented), `in-progress` (partial), `not-started`
- A milestone with ALL tasks done is valid — it documents what exists
- Include effort estimates: `small` / `medium` / `large`
- Pay attention to the "Detected Gaps" section — these are real issues found by code analysis

## Output

Respond with valid JSON only:

```json
{
  "project_summary": "Brief description of the project and its current state",
  "milestones": [
    {
      "title": "Authentication & Security",
      "description": "Implement authentication and role-based access control",
      "tasks": [
        {
          "title": "JWT Authentication",
          "description": "JWT auth is fully working: token generation in src/auth/service.py, validation in middleware.",
          "status": "done",
          "files": ["src/auth/service.py", "src/auth/middleware.py"],
          "effort": "large",
          "labels": ["done"]
        },
        {
          "title": "Role-based access control",
          "description": "Roles exist in User model but no middleware enforces them. Need rbac middleware + admin route decorators.",
          "status": "not-started",
          "files": ["src/middleware/rbac.py", "src/api/admin/routes.py"],
          "effort": "medium",
          "labels": ["enhancement"],
          "existing_issue": null
        },
        {
          "title": "Add rate limiting",
          "description": "No rate limiting on any endpoint. Add Redis-based rate limiter.",
          "status": "not-started",
          "files": ["src/middleware/rate_limit.py"],
          "effort": "small",
          "labels": ["enhancement", "security"],
          "existing_issue": 15
        }
      ],
      "confidence": 0.9
    }
  ],
  "overall_confidence": 0.85
}
```
