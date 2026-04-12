"""Checklist for the flask_starter fixture repo.

Planted issues (the agent must catch the majority, not all):
- config.py:       hardcoded API_KEY and DATABASE_URL
- models.py:       mutable default arg (roles=[])
- app.py:          bare except clause
- app.py:          plaintext API key comparison (timing attack)
"""
from tests.golden_agents.checklist import Checklist

CHECKLIST = Checklist(
    repo_name="flask_starter",
    description="Small Flask app with planted security + quality issues",
    project_summary_must_mention=[
        # Either "flask" or "python" is enough — the LLM usually names the
        # framework rather than the language, which is acceptable.
        r"flask|python",
    ],
    min_findings=2,
    require_file_line=True,
    max_milestones=5,  # matches the "0 to 5" upper bound in the grouping prompt
    banned_milestone_titles=[
        r"testing.*qa",
        r"ci/?cd",
        r"^documentation$",
        r"^add (?:tests|ci|docs)",
    ],
    must_mention=[
        # At least one of the planted issues must be surfaced.
        # Using alternation so the agent can describe them in its own words.
        r"(?:bare\s*except|hardcoded|mutable\s*default|api[_ -]?key)",
    ],
    must_not_mention=[
        # Week-1 v1 boilerplate patterns we never want to see again.
        # Note: bare "generic" is too loose — LLMs legitimately say things
        # like "generic error handling". We match the specific v1 phrases.
        r"add unit tests",
        r"set up (?:ci|cd)",
        r"improve (?:test )?coverage",
        r"add (?:more )?documentation",
    ],
)
