"""Checklist for the seeded_buggy fixture repo.

Dense with planted issues — the checklist demands concrete findings.

Planted issues:
- db.py:      SQL injection via f-string (2 places)
- db.py:      SQL injection via string concat
- db.py:      hardcoded DB_PASSWORD
- db.py:      missing commit after delete
- auth.py:    commented-out auth check (always returns True)
- auth.py:    plaintext password comparison
- auth.py:    bare except in login
- utils.py:   mutable default argument in accumulate
- utils.py:   silent exception swallow in safe_parse_int
"""
from tests.golden_agents.checklist import Checklist

CHECKLIST = Checklist(
    repo_name="seeded_buggy",
    description="Synthetic Python repo packed with classic bugs",
    project_summary_must_mention=[
        r"python",
    ],
    # Dense fixture — demand more findings.
    min_findings=3,
    require_file_line=True,
    max_milestones=5,  # matches the "0 to 5" upper bound in the grouping prompt
    banned_milestone_titles=[
        r"testing.*qa",
        r"ci/?cd",
        r"^documentation$",
        r"^add (?:tests|ci|docs)",
    ],
    must_mention=[
        # SQL injection is the most egregious planted issue; any reasonable
        # agent must catch it.
        r"sql\s*injection|format[ -]?string|string\s*concat",
        # And at least one of these other planted issues must surface.
        r"(?:bare\s*except|hardcoded|mutable\s*default|plaintext\s*password|commented[- ]?out)",
    ],
    must_not_mention=[
        r"add unit tests",
        r"set up (?:ci|cd)",
        r"improve (?:test )?coverage",
        r"add (?:more )?documentation",
    ],
)
