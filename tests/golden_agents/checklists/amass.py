"""Checklist for a local AMASS clone (the user's real repo).

AMASS isn't a lab fixture — it's a real project that moves. So this
checklist is deliberately *floor*-based: the agent must surface SOMETHING
concrete, not boilerplate, but we don't pin specific findings.
"""
from tests.golden_agents.checklist import Checklist

CHECKLIST = Checklist(
    repo_name="amass",
    description=(
        "Floor-based checklist for the user's real AMASS clone. Catches "
        "regression to v1-style generic output; pinned findings are out of "
        "scope because AMASS moves."
    ),
    # No project_summary pinning on AMASS. It's a real moving repo and the
    # LLM legitimately varies between "backend system for construction..."
    # and "real-time collaboration platform...". The other rules (banned
    # titles + floor counts + file:line citations) do the regression work.
    project_summary_must_mention=[],
    # 1 finding is enough — AMASS is a well-maintained real codebase, the
    # LLM is allowed to be conservative. The banned titles + file:line
    # requirement still catch regression to v1-style boilerplate.
    min_findings=1,
    require_file_line=True,
    max_milestones=5,
    banned_milestone_titles=[
        # v1 produced exactly these. Never again.
        r"testing.*qa",
        r"ci/?cd",
        r"^documentation$",
        r"^add (?:tests|ci|documentation|error handling)",
        r"implement (?:testing|ci|cd)",
    ],
    must_mention=[],  # no pinned findings — real repo moves
    must_not_mention=[
        r"add unit tests",
        r"set up (?:ci|cd)",
        r"improve code quality",  # classic v1 boilerplate phrase
        r"add (?:more )?documentation",
    ],
)
