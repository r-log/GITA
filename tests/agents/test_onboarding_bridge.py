"""Tests for the OnboardingResult → list[Decision] bridges.

Two bridges live in ``gita.agents.onboarding``:

1. ``build_onboarding_comment_decision`` — one Decision wrapping the whole
   OnboardingResult as a single comment. Shipped in Week 2 Day 7 and already
   covered end-to-end by the first real GitHub post; this file tests the
   pure function shape only.
2. ``build_onboarding_issue_decisions`` — one Decision *per milestone*,
   creating real issues. This is Week 3 Day 4's new surface — most of this
   file exists to prove the bridge does the right thing under every
   OnboardingResult shape the agent can produce.

Together with Day 2's dedupe integration tests, these are what lets Day 7
flip ``WRITE_MODE=full`` without holding breath: if the bridge is wrong,
every downstream test can still be green and we'd still create bad issues.
"""
from __future__ import annotations

from gita.agents.onboarding import (
    _COLLAPSE_THRESHOLD,
    _render_comment_body,
    _render_issue_body,
    build_onboarding_comment_decision,
    build_onboarding_issue_decisions,
)
from gita.agents.types import Finding, Milestone, OnboardingResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _result(
    findings: list[Finding] | None = None,
    milestones: list[Milestone] | None = None,
    project_summary: str = "A Flask app.",
    repo_name: str = "r-log/flask_starter",
    confidence: float = 0.85,
) -> OnboardingResult:
    return OnboardingResult(
        repo_name=repo_name,
        project_summary=project_summary,
        findings=findings or [],
        milestones=milestones or [],
        confidence=confidence,
    )


def _finding(
    file: str = "app.py",
    line: int = 23,
    severity: str = "high",
    kind: str = "security",
    description: str = "bare except swallows exceptions",
    fix_sketch: str = "",
) -> Finding:
    return Finding(
        file=file,
        line=line,
        severity=severity,
        kind=kind,
        description=description,
        fix_sketch=fix_sketch,
    )


def _milestone(
    title: str = "Harden error handling",
    summary: str = "Replace bare excepts with typed handlers.",
    finding_indices: list[int] | None = None,
    confidence: float = 0.8,
) -> Milestone:
    return Milestone(
        title=title,
        summary=summary,
        finding_indices=finding_indices if finding_indices is not None else [0],
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# build_onboarding_issue_decisions — empty cases
# ---------------------------------------------------------------------------
class TestEmptyResults:
    def test_no_milestones_returns_empty_list(self):
        result = _result(findings=[_finding()], milestones=[])
        decisions = build_onboarding_issue_decisions(
            result, target_repo="r-log/amass"
        )
        assert decisions == []

    def test_milestone_with_all_invalid_indices_is_skipped(self):
        """If a milestone only references out-of-range finding indices, the
        bridge drops it rather than producing an empty issue body."""
        result = _result(
            findings=[_finding()],
            milestones=[_milestone(finding_indices=[99, 100])],
        )
        decisions = build_onboarding_issue_decisions(
            result, target_repo="r-log/amass"
        )
        assert decisions == []

    def test_milestone_with_mixed_indices_keeps_valid_ones(self):
        result = _result(
            findings=[_finding(), _finding(file="auth.py", line=17)],
            milestones=[_milestone(finding_indices=[0, 99, 1])],
        )
        decisions = build_onboarding_issue_decisions(
            result, target_repo="r-log/amass"
        )
        assert len(decisions) == 1
        # Evidence lists only the two in-range findings (and three prefix lines).
        assert len(decisions[0].evidence) == 3 + 2


# ---------------------------------------------------------------------------
# build_onboarding_issue_decisions — happy path
# ---------------------------------------------------------------------------
class TestHappyPath:
    def test_one_decision_per_milestone(self):
        result = _result(
            findings=[_finding(), _finding(file="auth.py")],
            milestones=[
                _milestone(title="m1", finding_indices=[0]),
                _milestone(title="m2", finding_indices=[1]),
            ],
        )
        decisions = build_onboarding_issue_decisions(
            result, target_repo="r-log/amass"
        )
        assert len(decisions) == 2
        assert [d.payload["title"] for d in decisions] == ["m1", "m2"]

    def test_decision_action_is_create_issue(self):
        result = _result(
            findings=[_finding()], milestones=[_milestone()]
        )
        decisions = build_onboarding_issue_decisions(
            result, target_repo="r-log/amass"
        )
        assert decisions[0].action == "create_issue"

    def test_target_uses_target_repo_not_source_repo(self):
        """The onboarding source repo and the issue-creation target repo can
        differ (that's how ``--create-issues owner/other-repo`` works)."""
        result = _result(
            findings=[_finding()],
            milestones=[_milestone()],
            repo_name="synthetic_py",  # the source
        )
        decisions = build_onboarding_issue_decisions(
            result, target_repo="r-log/amass"  # the target
        )
        assert decisions[0].target["repo"] == "r-log/amass"
        # fallback_issue is absent when none was provided.
        assert "fallback_issue" not in decisions[0].target

    def test_target_no_issue_key(self):
        """create_issue is creating the issue, so target.issue must not be
        set — that's the shape GithubClient._create_issue expects."""
        result = _result(
            findings=[_finding()], milestones=[_milestone()]
        )
        decisions = build_onboarding_issue_decisions(
            result, target_repo="r-log/amass"
        )
        assert "issue" not in decisions[0].target

    def test_confidence_inherits_from_milestone(self):
        result = _result(
            findings=[_finding()],
            milestones=[_milestone(confidence=0.42)],
            confidence=0.9,  # overall, should NOT be copied
        )
        decisions = build_onboarding_issue_decisions(
            result, target_repo="r-log/amass"
        )
        assert decisions[0].confidence == 0.42

    def test_payload_has_title_and_body(self):
        result = _result(
            findings=[_finding()],
            milestones=[_milestone(title="Harden error handling")],
        )
        decisions = build_onboarding_issue_decisions(
            result, target_repo="r-log/amass"
        )
        assert decisions[0].payload["title"] == "Harden error handling"
        assert "body" in decisions[0].payload
        assert decisions[0].payload["body"]  # non-empty

    def test_labels_omitted_by_default(self):
        result = _result(
            findings=[_finding()], milestones=[_milestone()]
        )
        decisions = build_onboarding_issue_decisions(
            result, target_repo="r-log/amass"
        )
        assert "labels" not in decisions[0].payload

    def test_default_labels_plumbed_through(self):
        result = _result(
            findings=[_finding()], milestones=[_milestone()]
        )
        decisions = build_onboarding_issue_decisions(
            result,
            target_repo="r-log/amass",
            default_labels=["gita", "onboarding"],
        )
        assert decisions[0].payload["labels"] == ["gita", "onboarding"]


# ---------------------------------------------------------------------------
# Fallback comment target (required for comment-mode downgrade tests)
# ---------------------------------------------------------------------------
class TestFallbackCommentTarget:
    def test_fallback_issue_stored_in_target(self):
        result = _result(
            findings=[_finding()], milestones=[_milestone()]
        )
        decisions = build_onboarding_issue_decisions(
            result,
            target_repo="r-log/amass",
            fallback_comment_target=255,
        )
        assert decisions[0].target["fallback_issue"] == 255
        assert decisions[0].target["repo"] == "r-log/amass"


# ---------------------------------------------------------------------------
# Evidence chain
# ---------------------------------------------------------------------------
class TestEvidence:
    def test_evidence_has_milestone_and_confidence_prefix(self):
        result = _result(
            findings=[_finding()],
            milestones=[_milestone(title="m1")],
            confidence=0.92,
        )
        decisions = build_onboarding_issue_decisions(
            result, target_repo="r-log/amass"
        )
        evidence = decisions[0].evidence
        assert any("milestone: m1" in e for e in evidence)
        assert any("0.92" in e for e in evidence)

    def test_evidence_lists_one_bullet_per_cited_finding(self):
        result = _result(
            findings=[
                _finding(file="app.py", line=1),
                _finding(file="auth.py", line=2, severity="medium"),
                _finding(file="db.py", line=3, severity="critical"),
            ],
            milestones=[_milestone(finding_indices=[0, 2])],
        )
        decisions = build_onboarding_issue_decisions(
            result, target_repo="r-log/amass"
        )
        evidence = decisions[0].evidence
        assert any("app.py:1" in e for e in evidence)
        assert any("db.py:3" in e for e in evidence)
        # auth.py is not cited by this milestone.
        assert not any("auth.py:2" in e for e in evidence)


# ---------------------------------------------------------------------------
# Body renderer
# ---------------------------------------------------------------------------
class TestIssueBodyRenderer:
    def test_body_has_summary_and_checklist(self):
        finding = _finding(
            file="app.py",
            line=23,
            severity="high",
            kind="security",
            description="bare except",
        )
        milestone = _milestone(
            title="m",
            summary="Replace bare excepts.",
            finding_indices=[0],
        )
        body = _render_issue_body(milestone, [finding], "r-log/flask_starter")

        assert "Replace bare excepts." in body
        assert "- [ ]" in body
        assert "HIGH" in body
        assert "`app.py:23`" in body
        assert "bare except" in body
        assert "r-log/flask_starter" in body  # source repo in footer

    def test_body_only_includes_cited_findings(self):
        findings = [
            _finding(file="app.py", line=1),
            _finding(file="db.py", line=2),
        ]
        milestone = _milestone(finding_indices=[0])  # only cites app.py
        body = _render_issue_body(
            milestone, findings, "r-log/flask_starter"
        )
        assert "app.py:1" in body
        assert "db.py:2" not in body

    def test_body_without_findings_skips_heading(self):
        """Defensive: if somehow all cited indices are invalid, the renderer
        should still produce a valid (if sparse) body."""
        milestone = _milestone(finding_indices=[99])  # all out-of-range
        body = _render_issue_body(milestone, [], "r-log/flask_starter")
        assert "## Findings" not in body
        assert milestone.summary in body

    def test_body_includes_fix_sketch_when_present(self):
        finding = _finding(fix_sketch="Use try/except ValueError")
        milestone = _milestone(finding_indices=[0])
        body = _render_issue_body(milestone, [finding], "r-log/flask_starter")
        assert "try/except ValueError" in body


# ---------------------------------------------------------------------------
# Sanity: the Week 2 comment bridge still works (contract regression)
# ---------------------------------------------------------------------------
class TestCommentBridgeRegression:
    def test_comment_decision_shape_unchanged(self):
        """The Week 2 comment bridge is still used by Day 7's `--post-to`
        flow. Make sure Day 4's issue bridge didn't break the shared file."""
        result = _result(
            findings=[_finding()], milestones=[_milestone()]
        )
        decision = build_onboarding_comment_decision(
            result,
            repo_full_name="r-log/amass",
            issue_number=255,
        )
        assert decision.action == "comment"
        assert decision.target == {"repo": "r-log/amass", "issue": 255}
        assert "body" in decision.payload
        assert decision.confidence == 0.85


# ---------------------------------------------------------------------------
# P5: long-body <details> wrapping
# ---------------------------------------------------------------------------
class TestDetailsWrapping:
    def test_short_comment_body_is_not_wrapped(self):
        """Below the threshold, no <details> collapse."""
        findings = [
            _finding(file=f"f{i}.py", line=i + 1)
            for i in range(_COLLAPSE_THRESHOLD - 1)
        ]
        milestones = [
            _milestone(
                title=f"m{i}",
                finding_indices=[i],
            )
            for i in range(_COLLAPSE_THRESHOLD - 1)
        ]
        body = _render_comment_body(
            _result(findings=findings, milestones=milestones)
        )
        assert "<details>" not in body
        assert "Findings" in body
        assert "Milestone" in body

    def test_long_comment_body_wraps_findings(self):
        """At the threshold, findings collapse into <details>."""
        findings = [
            _finding(file=f"f{i}.py", line=i + 1)
            for i in range(_COLLAPSE_THRESHOLD)
        ]
        body = _render_comment_body(
            _result(findings=findings, milestones=[])
        )
        assert "<details>" in body
        assert "</details>" in body
        assert "<summary>" in body
        assert f"Findings ({_COLLAPSE_THRESHOLD})" in body
        assert "f0.py" in body
        assert f"f{_COLLAPSE_THRESHOLD - 1}.py" in body

    def test_long_comment_body_wraps_milestones(self):
        milestones = [
            _milestone(title=f"m{i}", finding_indices=[0])
            for i in range(_COLLAPSE_THRESHOLD)
        ]
        body = _render_comment_body(
            _result(findings=[_finding()], milestones=milestones)
        )
        assert body.count("<details>") == 1
        assert f"Milestones ({_COLLAPSE_THRESHOLD})" in body

    def test_long_issue_body_wraps_findings_checklist(self):
        """The per-milestone issue body collapses when a single milestone
        cites many findings. Small milestones render inline."""
        findings = [
            _finding(file=f"f{i}.py", line=i + 1)
            for i in range(_COLLAPSE_THRESHOLD)
        ]
        milestone = _milestone(
            finding_indices=list(range(_COLLAPSE_THRESHOLD))
        )
        body = _render_issue_body(
            milestone, findings, "r-log/flask_starter"
        )
        assert "<details>" in body
        assert "</details>" in body
        # Every finding still appears inside the wrapped block.
        for i in range(_COLLAPSE_THRESHOLD):
            assert f"f{i}.py:{i + 1}" in body

    def test_short_issue_body_unchanged(self):
        findings = [_finding()]
        milestone = _milestone(finding_indices=[0])
        body = _render_issue_body(
            milestone, findings, "r-log/flask_starter"
        )
        assert "<details>" not in body
        assert "Findings" in body

    def test_collapsed_blocks_have_blank_lines_around_summary(self):
        """Without a blank line after <summary>, GitHub's markdown renderer
        won't parse the inner content. Day 5 acceptance bar."""
        findings = [
            _finding(file=f"f{i}.py", line=i + 1)
            for i in range(_COLLAPSE_THRESHOLD)
        ]
        body = _render_comment_body(
            _result(findings=findings, milestones=[])
        )
        lines = body.split("\n")
        summary_idx = next(
            i for i, line in enumerate(lines) if "<summary>" in line
        )
        # The line right after <summary> must be blank.
        assert lines[summary_idx + 1] == ""
