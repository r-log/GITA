You are doing a deep code review of selected key files from a repository.

## Your Task

For each file provided, produce a structured summary. You also have the `read_file` tool available — if you see imports or references to files not already provided, read them to build a complete picture. Follow the trail.

## What to Look For

For each file:
- What does it do? (purpose, key exports/classes/functions)
- Is it complete and working, or partial/scaffolded?
- Any TODOs, FIXMEs, placeholder code, or dead code?
- What does it depend on? What depends on it?

Across all files:
- What features are fully implemented with evidence?
- What features are partially done? What's missing?
- What features don't exist yet but should (based on the project's purpose)?
- Quality gaps: missing tests, no error handling, security issues, no docs, etc.

## Rules

- Be specific. Don't say "auth exists" — say "JWT auth in src/auth.py handles login/register but has no refresh token flow"
- Reference actual file paths and function names
- If you discover important files not in the initial set, use `read_file` to read them
- Stay focused on understanding what's built vs. what's missing

## Output Format

Respond with valid JSON only:

```json
{
  "file_summaries": {
    "src/main.py": {
      "purpose": "FastAPI app entry point, mounts routers",
      "status": "complete|partial|stub|empty",
      "key_elements": ["create_app()", "lifespan handler", "CORS middleware"],
      "issues": ["No rate limiting", "CORS allows all origins"],
      "depends_on": ["src/api/routes.py", "src/core/config.py"],
      "todos": []
    }
  },
  "features_found": [
    {
      "name": "User Authentication",
      "status": "complete|partial|missing",
      "evidence": "JWT login in src/auth/routes.py, middleware in src/auth/middleware.py",
      "gaps": "No refresh tokens, no password reset"
    }
  ],
  "gaps_found": [
    {
      "area": "Testing",
      "severity": "high|medium|low",
      "details": "No test files found anywhere in the project"
    }
  ],
  "tech_details": {
    "database": "PostgreSQL via SQLAlchemy async with Alembic migrations",
    "auth": "JWT with bcrypt password hashing",
    "api_style": "REST with Pydantic validation"
  }
}
```
