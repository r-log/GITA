"""Tests for the PR reviewer Decision bridge.

Pure tests — no DB, no LLM, no GitHub. The bridge takes a PRReviewResult
and produces a Decision; these tests verify the shape.
"""
from __future__ import annotations

from gita.agents.pr_reviewer.bridge import (
    _render_review_body,
    build_pr_review_decision,
)
from gita.agents.types import Finding, PRReviewResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _finding(
    file: str = "src/db.py",
    line: int = 42,
    severity: str = "high",
    kind: str = "security",
    description: str = "SQL injection via f-string",
    fix_sketch: str = "Use parameterized query",
) -> Finding:
    return Finding(
        file=file, line=line, severity=severity, kind=kind,
        description=description, fix_sketch=fix_sketch,
    )


def _result(
    findings: list[Finding] | None = None,
    summary: str = "Found a SQL injection in the changed code.",
    verdict: str = "request_changes",
    confidence: float = 0.9,
) -> PRReviewResult:
    return PRReviewResult(
        repo_name="r-log/amass",
        pr_number=42,
        pr_title="Fix user query",
        summary=summary,
        verdict=verdict,
        findings=findings or [],
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Decision shape
# ---------------------------------------------------------------------------
class TestBuildDecision:
    def test_action_is_comment(self):
        decision = build_pr_review_decision(
            _result(findings=[_finding()]),
            repo_full_name="r-log/amass",
            pr_number=42,
        )
        assert decision.action == "comment"

    def test_target_uses_pr_number_as_issue(self):
        """GitHub treats PR comments as issue comments — same endpoint."""
        decision = build_pr_review_decision(
            _result(), repo_full_name="r-log/amass", pr_number=42
        )
        assert decision.target == {"repo": "r-log/amass", "issue": 42}

    def test_confidence_inherits_from_result(self):
        decision = build_pr_review_decision(
            _result(confidence=0.77),
            repo_full_name="r-log/amass",
            pr_number=42,
        )
        assert decision.confidence == 0.77

    def test_evidence_includes_verdict_and_count(self):
        decision = build_pr_review_decision(
            _result(findings=[_finding()], verdict="request_changes"),
            repo_full_name="r-log/amass",
            pr_number=42,
        )
        evidence = decision.evidence
        assert any("request_changes" in e for e in evidence)
        assert any("1 findings" in e for e in evidence)

    def test_evidence_includes_top_findings(self):
        decision = build_pr_review_decision(
            _result(findings=[
                _finding(file="a.py", line=1),
                _finding(file="b.py", line=2),
            ]),
            repo_full_name="r-log/amass",
            pr_number=42,
        )
        evidence = decision.evidence
        assert any("a.py:1" in e for e in evidence)
        assert any("b.py:2" in e for e in evidence)

    def test_payload_body_is_nonempty(self):
        decision = build_pr_review_decision(
            _result(findings=[_finding()]),
            repo_full_name="r-log/amass",
            pr_number=42,
        )
        assert decision.payload["body"]
        assert len(decision.payload["body"]) > 50


# ---------------------------------------------------------------------------
# Body renderer
# ---------------------------------------------------------------------------
class TestRenderReviewBody:
    def test_body_has_pr_header(self):
        body = _render_review_body(_result())
        assert "#42" in body

    def test_body_has_verdict(self):
        body = _render_review_body(_result(verdict="request_changes"))
        assert "Changes Requested" in body

    def test_body_has_approve_verdict(self):
        body = _render_review_body(_result(findings=[], verdict="approve"))
        assert "Approved" in body

    def test_body_has_summary_in_blockquote(self):
        body = _render_review_body(_result(
            summary="Found a SQL injection."
        ))
        assert "> Found a SQL injection." in body

    def test_body_has_findings_grouped_by_file(self):
        body = _render_review_body(_result(findings=[
            _finding(file="src/db.py", line=42),
            _finding(file="src/db.py", line=50, description="another issue"),
            _finding(file="src/auth.py", line=10),
        ]))
        assert "`src/db.py`" in body
        assert "`src/auth.py`" in body
        assert "SQL injection" in body
        assert "another issue" in body

    def test_finding_card_has_severity_and_location(self):
        body = _render_review_body(_result(findings=[_finding()]))
        assert "HIGH" in body
        assert "`src/db.py:42`" in body

    def test_body_includes_fix_sketch(self):
        body = _render_review_body(_result(findings=[_finding()]))
        assert "Use parameterized query" in body

    def test_body_has_footer(self):
        body = _render_review_body(_result(confidence=0.88))
        assert "GITA v0.1.0" in body
        assert "88%" in body

    def test_empty_findings_says_no_issues(self):
        body = _render_review_body(_result(findings=[], verdict="approve"))
        assert "No issues found" in body

    def test_finding_count_in_header(self):
        body = _render_review_body(_result(findings=[
            _finding(), _finding(file="b.py"),
        ]))
        assert "2 findings" in body

    def test_single_finding_no_plural(self):
        body = _render_review_body(_result(findings=[_finding()]))
        assert "1 finding" in body

    def test_many_findings_collapsed(self):
        findings = [
            _finding(file=f"f{i}.py", line=i + 1)
            for i in range(7)
        ]
        body = _render_review_body(_result(findings=findings))
        assert "<details>" in body
        assert "</details>" in body
        assert "click to expand" in body

    def test_few_findings_not_collapsed(self):
        findings = [_finding(), _finding(file="other.py", line=5)]
        body = _render_review_body(_result(findings=findings))
        assert "<details>" not in body
