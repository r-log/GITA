"""PR reviewer agent — Week 4.

Submodules:
- ``diff_parser``  — parse GitHub PR file diffs into structured hunks
- ``schemas``      — pydantic LLM I/O models
- ``recipe``       — the two-call review pipeline

Public API re-exported here.
"""
from gita.agents.pr_reviewer.diff_parser import (
    ChangedLineRange,
    DiffHunk,
    parse_pr_files,
)
from gita.agents.pr_reviewer.recipe import PRReviewError, run_pr_review
from gita.agents.pr_reviewer.schemas import (
    FindingsResponse,
    ReviewSummaryResponse,
)

__all__ = [
    "ChangedLineRange",
    "DiffHunk",
    "parse_pr_files",
    "PRReviewError",
    "run_pr_review",
    "FindingsResponse",
    "ReviewSummaryResponse",
]
