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
    project_summary_must_mention=[
        # AMASS is a Python + JavaScript full-stack app. Any onboarding
        # output that doesn't mention at least one of these is missing
        # the tech stack entirely.
        r"python|flask|javascript|backend",
    ],
    min_findings=2,
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
        r"\bgeneric\b",
        r"add unit tests",
        r"set up ci/cd",
        r"improve code quality",  # classic v1 boilerplate phrase
    ],
)
