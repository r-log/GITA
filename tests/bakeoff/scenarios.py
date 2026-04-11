"""
All test scenarios for the bake-off, organized by workload type.
Each scenario defines: system prompt, user message, tools (if any),
expected behavior, and scoring criteria.
"""

# ── W1: Tool Calling (Short, 3-5 calls) ─────────────────────────

W1_SHORT_TOOL_CALLING = [
    {
        "id": "W1.1",
        "name": "PR Review (simple)",
        "workload": "W1_short_tools",
        "system_prompt": (
            "You are a PR reviewer. Analyze the PR, check linked issue, "
            "post a review comment, save analysis, create check run.\n\n"
            "CONTEXT:\n"
            "- Repo: test-owner/test-repo\n"
            "- PR #87: 'Fix OAuth callback redirect on Safari'\n"
            "- PR body: 'Fixes #42. Changed SameSite from Strict to Lax.'\n"
            "- Diff: 2 files, 15 additions, 3 deletions\n"
            "- No test files modified\n\n"
            "Do: fetch issue, post comment, save analysis, create check run."
        ),
        "user_message": "Begin your analysis.",
        "use_tools": True,
        "expected_tools": {"get_issue", "post_comment", "save_analysis", "create_check_run"},
        "max_expected_calls": 8,
    },
    {
        "id": "W1.2",
        "name": "Issue triage",
        "workload": "W1_short_tools",
        "system_prompt": (
            "You are an issue analyst. A new issue was opened.\n\n"
            "CONTEXT:\n"
            "- Repo: test-owner/test-repo\n"
            "- Issue #42: 'OAuth login fails on Safari'\n\n"
            "Do: fetch full issue, search for related comments, save analysis, post comment."
        ),
        "user_message": "Begin your analysis.",
        "use_tools": True,
        "expected_tools": {"get_issue_full", "save_analysis", "post_comment"},
        "max_expected_calls": 8,
    },
]


# ── W2: Tool Calling (Long, 15+ calls) ──────────────────────────

W2_LONG_TOOL_CALLING = [
    {
        "id": "W2.1",
        "name": "Deep investigation (15+ calls)",
        "workload": "W2_long_tools",
        "system_prompt": (
            "You are investigating a complex issue that spans multiple files and PRs.\n\n"
            "CONTEXT:\n"
            "- Repo: test-owner/test-repo\n"
            "- Issue #42: 'OAuth login fails on Safari'\n"
            "- Related issues might exist: #10, #23, #35\n"
            "- Multiple PRs may have touched auth code\n\n"
            "Your job (DO ALL OF THESE, one at a time):\n"
            "1. Fetch full details of issue #42\n"
            "2. Search comments on issue #42\n"
            "3. Search events for issue #42\n"
            "4. Fetch issue #10 to check if related\n"
            "5. Fetch issue #23 to check if related\n"
            "6. Fetch issue #35 to check if related\n"
            "7. Search comments for keyword 'OAuth'\n"
            "8. Search comments for keyword 'Safari'\n"
            "9. Search comments for keyword 'cookie'\n"
            "10. Search events for PRs\n"
            "11. Fetch PR #87 if found\n"
            "12. Save a comprehensive analysis\n"
            "13. Post a detailed comment with all findings\n\n"
            "Execute each step in order. Do NOT skip steps."
        ),
        "user_message": "Begin the full investigation now. Execute all 13 steps.",
        "use_tools": True,
        "expected_tools": {"get_issue_full", "get_issue", "search_comments", "search_events", "get_pr", "save_analysis", "post_comment"},
        "max_expected_calls": 20,
    },
]


# ── W3: Code Analysis ────────────────────────────────────────────

W3_CODE_ANALYSIS = [
    {
        "id": "W3.1",
        "name": "Diff quality assessment",
        "workload": "W3_code_analysis",
        "system_prompt": "You are a code reviewer. Analyze this diff for quality, security, and correctness.",
        "user_message": (
            "Review this diff:\n\n"
            "```diff\n"
            "--- a/auth/oauth.py\n"
            "+++ b/auth/oauth.py\n"
            "@@ -45,7 +45,7 @@ def handle_callback(request):\n"
            "     token = exchange_code(code)\n"
            "-    response.set_cookie('session', token, samesite='Strict', secure=True)\n"
            "+    response.set_cookie('session', token, samesite='Lax', secure=True, httponly=True)\n"
            "     return redirect('/dashboard')\n\n"
            "--- a/auth/config.py\n"
            "+++ b/auth/config.py\n"
            "@@ -12,6 +12,7 @@ OAUTH_CONFIG = {\n"
            "+    'COOKIE_SAMESITE': 'Lax',\n"
            "     'REDIRECT_URI': '/auth/callback',\n"
            "```\n\n"
            "Respond with:\n"
            "1. Quality assessment (good/needs-work/critical)\n"
            "2. Security implications\n"
            "3. Missing: any concerns about test coverage?\n"
            "4. Recommendation: approve/request-changes\n"
            "Format as structured JSON."
        ),
        "use_tools": False,
        "expected_keys": ["quality", "security", "recommendation"],
    },
]


# ── W4: S.M.A.R.T. Evaluation ───────────────────────────────────

W4_SMART_EVAL = [
    {
        "id": "W4.1",
        "name": "S.M.A.R.T. issue evaluation",
        "workload": "W4_smart_eval",
        "system_prompt": "You evaluate GitHub issues against S.M.A.R.T. criteria (Specific, Measurable, Achievable, Relevant, Time-bound). Score each criterion 1-10 and give suggestions.",
        "user_message": (
            "Evaluate this issue:\n\n"
            "**Title:** OAuth login fails on Safari\n\n"
            "**Body:** Users on Safari get stuck on the callback page after OAuth login. "
            "The page spins indefinitely. Works fine on Chrome and Firefox.\n\n"
            "**Labels:** bug, auth\n"
            "**Assignees:** none\n"
            "**Milestone:** none\n\n"
            "Respond with JSON containing: specific_score, measurable_score, achievable_score, "
            "relevant_score, time_bound_score, overall_score, suggestions (array of strings)."
        ),
        "use_tools": False,
        "expected_keys": ["specific_score", "measurable_score", "overall_score", "suggestions"],
    },
]


# ── W5: Project Planning ─────────────────────────────────────────

W5_PROJECT_PLANNING = [
    {
        "id": "W5.1",
        "name": "Milestone inference from code map",
        "workload": "W5_project_planning",
        "system_prompt": "You are a project planner. Given a code map of a repository, propose 3-5 milestones with sub-issues for each.",
        "user_message": (
            "Here is the code map for project 'acme/webapp':\n\n"
            "**Languages:** python (45), javascript (30), typescript (20), yaml (5)\n"
            "**Total files:** 100 | **Total lines:** 15,000\n\n"
            "## API Routes\n"
            "**backend/api/auth.py:** POST /api/login -> login(), POST /api/register -> register()\n"
            "**backend/api/users.py:** GET /api/users -> list_users(), GET /api/users/{id} -> get_user()\n\n"
            "## Models\n"
            "- **User** (Base) [backend/models/user.py] Fields: id, email, name\n"
            "- **Session** (Base) [backend/models/session.py] Fields: id, user_id, token\n\n"
            "## Repository Facts\n"
            "- total_source_files: 100\n"
            "- test_files: 0\n"
            "- dockerfile_present: False\n"
            "- github_workflows: 0\n"
            "- readme_present: False\n"
            "- env_example_present: False\n"
            "- env_file_present: True\n\n"
            "Propose milestones as JSON: [{title, description, sub_issues: [{title, description, labels}]}]"
        ),
        "use_tools": False,
        "expected_keys": ["title", "sub_issues"],
    },
]


# ── W6: Comment Writing Quality ──────────────────────────────────

W6_COMMENT_WRITING = [
    {
        "id": "W6.1",
        "name": "PR review comment",
        "workload": "W6_comment_writing",
        "system_prompt": "You are a GitHub bot that writes PR review comments. Be helpful, concise, professional. Use markdown. Include specific code references.",
        "user_message": (
            "Write a review comment for this PR:\n\n"
            "**PR #87:** Fix OAuth callback redirect on Safari\n"
            "**Issue:** #42 (OAuth login fails on Safari)\n"
            "**Changes:** SameSite cookie changed from Strict to Lax, added httponly flag\n"
            "**Files:** auth/oauth.py, auth/config.py\n"
            "**Tests:** None modified\n"
            "**Risk:** Low (auth-related but scoped change)\n\n"
            "The change looks correct technically. Main concern: no tests added.\n\n"
            "Write the comment the bot would post on the PR."
        ),
        "use_tools": False,
        "expected_keys": [],
    },
    {
        "id": "W6.2",
        "name": "Issue analysis comment",
        "workload": "W6_comment_writing",
        "system_prompt": "You are a GitHub bot that writes issue analysis comments. Be helpful and actionable. Use markdown.",
        "user_message": (
            "Write an analysis comment for this issue:\n\n"
            "**Issue #42:** OAuth login fails on Safari\n"
            "**S.M.A.R.T. Score:** 6/10\n"
            "**Findings:**\n"
            "- Specific: 8/10 (clear browser mentioned)\n"
            "- Measurable: 4/10 (no steps to reproduce)\n"
            "- Achievable: 7/10 (scoped to one browser)\n"
            "- Relevant: 9/10 (auth is critical)\n"
            "- Time-bound: 2/10 (no deadline or priority)\n\n"
            "Write the comment the bot would post."
        ),
        "use_tools": False,
        "expected_keys": [],
    },
]


# ── W7: Classification ───────────────────────────────────────────

W7_CLASSIFICATION = [
    {
        "id": "W7.1",
        "name": "Event severity classification",
        "workload": "W7_classification",
        "system_prompt": "Classify GitHub events. Respond with ONLY a JSON object: {severity: 'low'|'medium'|'high'|'critical', category: string, needs_agent: boolean}",
        "user_message": "Event: issues.opened\nTitle: 'Update README typo'\nBody: 'Found a typo in the README, line 42: \"teh\" should be \"the\"'\nLabels: none",
        "use_tools": False,
        "expected_keys": ["severity", "category", "needs_agent"],
    },
    {
        "id": "W7.2",
        "name": "Severity classification (critical)",
        "workload": "W7_classification",
        "system_prompt": "Classify GitHub events. Respond with ONLY a JSON object: {severity: 'low'|'medium'|'high'|'critical', category: string, needs_agent: boolean}",
        "user_message": "Event: issues.opened\nTitle: 'URGENT: API keys exposed in public repo'\nBody: 'Our AWS access keys are committed in config.py on the main branch. Anyone can see them.'\nLabels: security",
        "use_tools": False,
        "expected_keys": ["severity", "category", "needs_agent"],
    },
    {
        "id": "W7.3",
        "name": "Bug vs feature classification",
        "workload": "W7_classification",
        "system_prompt": "Classify this issue. Respond with ONLY a JSON object: {type: 'bug'|'feature'|'docs'|'chore', confidence: float 0-1}",
        "user_message": "Title: 'Add dark mode support'\nBody: 'It would be great to have a dark mode toggle in the settings page. Many users have requested this.'",
        "use_tools": False,
        "expected_keys": ["type", "confidence"],
    },
]


# ── W8: Summarization ────────────────────────────────────────────

W8_SUMMARIZATION = [
    {
        "id": "W8.1",
        "name": "Large diff summarization",
        "workload": "W8_summarization",
        "system_prompt": "Summarize code diffs into bullet points. Be concise. Focus on WHAT changed and WHY it matters. Max 5 bullet points.",
        "user_message": (
            "Summarize this diff (800 lines across 12 files):\n\n"
            "Files changed:\n"
            "- auth/oauth.py (+15, -3): Changed SameSite cookie from Strict to Lax\n"
            "- auth/config.py (+5, -0): Added COOKIE_SAMESITE config\n"
            "- auth/middleware.py (+20, -8): Refactored session validation\n"
            "- auth/tests/test_oauth.py (+45, -0): NEW - OAuth callback tests\n"
            "- auth/tests/test_session.py (+30, -5): Updated session tests\n"
            "- frontend/src/auth/callback.tsx (+10, -3): Updated redirect handling\n"
            "- frontend/src/auth/hooks.ts (+8, -2): Added error state handling\n"
            "- docs/auth.md (+25, -10): Updated OAuth documentation\n"
            "- docker-compose.yml (+3, -1): Added Redis for session store\n"
            "- requirements.txt (+2, -0): Added redis, hiredis deps\n"
            "- .env.example (+3, -0): Added REDIS_URL, SESSION_SECRET\n"
            "- CHANGELOG.md (+15, -0): Added v2.1.0 entry\n\n"
            "Total: +181, -32 across 12 files"
        ),
        "use_tools": False,
        "expected_keys": [],
    },
]


# ── All scenarios grouped ────────────────────────────────────────

ALL_SCENARIOS = (
    W1_SHORT_TOOL_CALLING +
    W2_LONG_TOOL_CALLING +
    W3_CODE_ANALYSIS +
    W4_SMART_EVAL +
    W5_PROJECT_PLANNING +
    W6_COMMENT_WRITING +
    W7_CLASSIFICATION +
    W8_SUMMARIZATION
)

WORKLOAD_GROUPS = {
    "W1_short_tools": W1_SHORT_TOOL_CALLING,
    "W2_long_tools": W2_LONG_TOOL_CALLING,
    "W3_code_analysis": W3_CODE_ANALYSIS,
    "W4_smart_eval": W4_SMART_EVAL,
    "W5_project_planning": W5_PROJECT_PLANNING,
    "W6_comment_writing": W6_COMMENT_WRITING,
    "W7_classification": W7_CLASSIFICATION,
    "W8_summarization": W8_SUMMARIZATION,
}
