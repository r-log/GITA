"""Unit tests for the checklist runner itself.

These don't touch the onboarding agent — they build a fake ``OnboardingResult``
and assert that ``check_output`` correctly catches / passes each rule.
Covers the checklist's own failure modes so Day 6 doesn't have to.
"""
from __future__ import annotations

from gita.agents.types import Finding, Milestone, OnboardingResult
from tests.golden_agents.checklist import Checklist, check_output


def _good_result() -> OnboardingResult:
    """A result that should satisfy a permissive checklist."""
    return OnboardingResult(
        repo_name="example",
        project_summary="A Flask web app written in Python.",
        findings=[
            Finding(
                file="app.py",
                line=47,
                severity="high",
                kind="security",
                description="Bare except clause swallows errors",
            ),
            Finding(
                file="config.py",
                line=3,
                severity="high",
                kind="security",
                description="Hardcoded API key in source",
            ),
        ],
        milestones=[
            Milestone(
                title="Security hardening",
                summary="Fix credential and error handling issues",
                finding_indices=[0, 1],
                confidence=0.9,
            ),
        ],
        confidence=0.85,
    )


def _permissive_checklist() -> Checklist:
    return Checklist(
        repo_name="example",
        project_summary_must_mention=[r"flask", r"python"],
        min_findings=1,
        require_file_line=True,
        max_milestones=3,
        banned_milestone_titles=[r"testing.*qa"],
        must_mention=[r"bare except"],
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
class TestChecklistPasses:
    def test_good_result_no_violations(self):
        violations = check_output(_good_result(), _permissive_checklist())
        assert violations == []

    def test_empty_checklist_passes_any_result(self):
        result = _good_result()
        checklist = Checklist(repo_name="example", min_findings=0)
        assert check_output(result, checklist) == []


# ---------------------------------------------------------------------------
# project_summary rules
# ---------------------------------------------------------------------------
class TestProjectSummaryRules:
    def test_missing_mention_is_flagged(self):
        result = _good_result()
        result.project_summary = "A web application"  # no flask, no python
        violations = check_output(result, _permissive_checklist())
        assert any("flask" in v.lower() for v in violations)
        assert any("python" in v.lower() for v in violations)

    def test_case_insensitive_mention(self):
        result = _good_result()
        result.project_summary = "Built on FLASK in PYTHON"
        assert check_output(result, _permissive_checklist()) == []


# ---------------------------------------------------------------------------
# Findings rules
# ---------------------------------------------------------------------------
class TestFindingsRules:
    def test_min_findings_enforced(self):
        result = _good_result()
        result.findings = []  # zero findings
        violations = check_output(result, _permissive_checklist())
        assert any("at least 1 findings" in v for v in violations)

    def test_missing_file_line_is_flagged(self):
        result = _good_result()
        result.findings[0] = Finding(
            file="",
            line=0,
            severity="high",
            kind="quality",
            description="Something vague",
        )
        violations = check_output(result, _permissive_checklist())
        assert any("file:line" in v for v in violations)

    def test_require_file_line_false_skips_check(self):
        result = _good_result()
        result.findings[0] = Finding(
            file="",
            line=0,
            severity="high",
            kind="quality",
            description="Bare except somewhere",
        )
        checklist = _permissive_checklist()
        checklist.require_file_line = False
        assert check_output(result, checklist) == []


# ---------------------------------------------------------------------------
# Milestones rules
# ---------------------------------------------------------------------------
class TestMilestoneRules:
    def test_too_many_milestones_flagged(self):
        result = _good_result()
        result.milestones = [
            Milestone(title=f"Milestone {i}", summary="...") for i in range(5)
        ]
        checklist = _permissive_checklist()
        checklist.max_milestones = 3
        violations = check_output(result, checklist)
        assert any("too many milestones" in v for v in violations)

    def test_banned_title_is_flagged(self):
        result = _good_result()
        result.milestones = [
            Milestone(title="Testing & QA", summary="...", confidence=0.5),
        ]
        violations = check_output(result, _permissive_checklist())
        assert any("banned milestone title" in v for v in violations)

    def test_banned_title_case_insensitive(self):
        result = _good_result()
        result.milestones = [
            Milestone(title="TESTING AND QA", summary="...", confidence=0.5),
        ]
        violations = check_output(result, _permissive_checklist())
        assert any("banned milestone title" in v for v in violations)


# ---------------------------------------------------------------------------
# must_mention / must_not_mention rules
# ---------------------------------------------------------------------------
class TestMentionRules:
    def test_must_mention_flagged_when_absent(self):
        result = _good_result()
        result.findings[0].description = "nothing special"
        result.findings[1].description = "nothing here either"
        violations = check_output(result, _permissive_checklist())
        assert any("must mention" in v for v in violations)

    def test_must_not_mention_flagged_when_present(self):
        result = _good_result()
        result.project_summary = "A generic Flask Python app with generic stuff"
        checklist = _permissive_checklist()
        checklist.must_not_mention = [r"\bgeneric\b"]
        violations = check_output(result, checklist)
        assert any("must NOT mention" in v for v in violations)

    def test_must_mention_matches_any_field(self):
        """A pattern should match across any field in the serialized output."""
        result = _good_result()
        result.project_summary = "A plain app"
        result.findings[0].description = "nothing"
        result.findings[1].description = "nothing"
        # Put the required phrase in a milestone summary instead
        result.milestones[0].summary = "Address the bare except clause"
        violations = check_output(result, _permissive_checklist())
        # Still passes — "bare except" appears in the milestone summary
        assert not any("must mention" in v for v in violations)


# ---------------------------------------------------------------------------
# Real checklist imports — smoke test
# ---------------------------------------------------------------------------
class TestRealChecklistsLoad:
    def test_amass_checklist_imports(self):
        from tests.golden_agents.checklists.amass import CHECKLIST

        assert CHECKLIST.repo_name == "amass"
        assert CHECKLIST.banned_milestone_titles  # non-empty

    def test_flask_starter_checklist_imports(self):
        from tests.golden_agents.checklists.flask_starter import CHECKLIST

        assert CHECKLIST.repo_name == "flask_starter"
        assert CHECKLIST.must_mention  # has pinned findings

    def test_seeded_buggy_checklist_imports(self):
        from tests.golden_agents.checklists.seeded_buggy import CHECKLIST

        assert CHECKLIST.repo_name == "seeded_buggy"
        assert CHECKLIST.min_findings >= 3  # dense fixture
