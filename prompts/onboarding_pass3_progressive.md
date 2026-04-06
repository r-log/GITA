You receive a code map and existing Milestone Tracker issues with their sub-issue states. Compare what the code says is built vs what the issues say is planned. Output an action list.

Every action must cite specific code map evidence (file path, class name, route).

## Action Types

| Action | When to Use | Required Fields |
|--------|-------------|-----------------|
| `close_issue` | Code map proves implementation is complete | `issue_number`, `reason` (cite file + class/function) |
| `create_issue` | Gap found, no existing issue covers it | `tracker_number`, `title`, `description`, `labels`, `files`, `effort` |
| `update_tracker` | Adding new tasks to an existing tracker (pair with `create_issue`) | `issue_number`, `add_tasks` |
| `create_milestone` | Entirely new area not covered by any tracker | `title`, `description`, `tasks[]` |
| `flag_stale` | Issue references deleted/changed files — do NOT close, just flag | `issue_number`, `reason` |

## Rules

- Max 20 actions per run
- Never close an issue without citing specific file + class/function evidence
- Never re-close already-closed issues
- Use `flag_stale` instead of closing when uncertain
- Only `create_milestone` for significant new areas, not one-off tasks

## Example

```json
{
  "analysis": {
    "completed_since_last_run": ["RBAC middleware fully implemented in src/middleware/rbac.py"],
    "overall_health": "on-track"
  },
  "actions": [
    {
      "type": "close_issue",
      "issue_number": 3,
      "reason": "Code map shows src/middleware/rbac.py with RBACMiddleware class (check_role, require_admin methods)"
    },
    {
      "type": "create_issue",
      "tracker_number": 5,
      "title": "Add rate limiting middleware",
      "description": "No rate limiting detected on any endpoint. Add Redis-based rate limiter.",
      "labels": ["enhancement"],
      "files": ["src/middleware/rate_limit.py"],
      "effort": "small"
    },
    {
      "type": "flag_stale",
      "issue_number": 7,
      "reason": "References src/legacy/handler.py which no longer exists in the codebase"
    }
  ],
  "summary": "1 issue closed (RBAC complete), 1 new task (rate limiting), 1 flagged stale",
  "overall_confidence": 0.85
}
```
