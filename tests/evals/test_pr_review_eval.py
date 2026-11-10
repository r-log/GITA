"""The PR-review eval's own correctness: cases, grader, and harness.

An eval that is wrong produces confident numbers pointing the wrong way,
so the harness gets the same treatment as product code:

* every case applies to the base repo, compiles, and its expected bugs
  resolve to exactly one place;
* the grader's matching and metrics behave as documented;
* an oracle reviewer scores 100% and a null reviewer finds nothing,
  through the real pipeline (index, guardrails, verdict).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from evals.pr_review.cases import (
    Case,
    CaseError,
    ExpectedBug,
    ExtraFinding,
    load_case,
    load_cases,
)
from evals.pr_review.grading import ReportedFinding, aggregate, grade_case, wilson
from evals.pr_review.run import main, model_matches


def _case(kind: str = "bug", expected: list[ExpectedBug] | None = None) -> Case:
    return Case(
        id="t",
        kind=kind,
        tags=[],
        title="t",
        body="",
        notes="",
        before={},
        after={"a.py": "x = 1\n"},
        expected=expected or [],
    )


def _bug(id_: str = "b", start: int = 10, end: int = 10, severity: str = "high") -> ExpectedBug:
    return ExpectedBug(
        id=id_, file="a.py", start_line=start, end_line=end, kinds=("bug",), severity=severity
    )


def _f(line: int, severity: str = "high", file: str = "a.py") -> ReportedFinding:
    return ReportedFinding(file=file, line=line, severity=severity, kind="bug")


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------
class TestCases:
    def test_every_case_loads_and_is_well_formed(self):
        cases = load_cases()
        kinds = [c.kind for c in cases]
        assert kinds.count("bug") >= 10 and kinds.count("clean") >= 5
        assert kinds.count("adversarial") >= 1
        assert len({c.id for c in cases}) == len(cases)
        for case in cases:
            files = case.pr_files_json()
            assert files and all(f["patch"].startswith("@@") for f in files), case.id

    def test_ground_truth_never_reaches_the_model(self):
        """Nothing the reviewer sees (title, body, code) may contain the
        case notes or name the expected bug."""
        for case in load_cases():
            visible = case.title + case.body + "".join(case.after.values())
            assert case.notes not in visible, case.id
            for bug in case.expected:
                assert not re.search(rf"\b{re.escape(bug.id)}\b", visible), (case.id, bug.id)


# ---------------------------------------------------------------------------
# Grader
# ---------------------------------------------------------------------------
class TestGrader:
    def test_match_within_tolerance_and_miss_outside(self):
        case = _case(expected=[_bug(start=10, end=11)])
        assert grade_case(case, [_f(13)], "request_changes").metrics["correct"] == 1.0
        missed = grade_case(case, [_f(14)], "request_changes")
        assert missed.metrics["correct"] == 0.0
        assert missed.metrics["false_alarms"] == 1.0

    def test_other_file_never_matches(self):
        case = _case(expected=[_bug()])
        assert grade_case(case, [_f(10, file="b.py")], "comment").metrics["found"] == 0.0

    def test_each_bug_matched_once_by_the_closest_finding(self):
        case = _case(expected=[_bug("x", 10, 10), _bug("y", 13, 13)])
        grade = grade_case(case, [_f(12), _f(10)], "request_changes")
        assert grade.matches["x"].line == 10 and grade.matches["y"].line == 12
        assert grade.metrics["found"] == 1.0

    def test_low_severity_extras_are_nits_not_false_alarms(self):
        grade = grade_case(_case("clean"), [_f(3, "low")], "comment")
        assert grade.metrics["correct"] == 1.0 and grade.nits and not grade.false_alarms

    def test_medium_extra_fails_a_clean_pr(self):
        grade = grade_case(_case("clean"), [_f(3, "medium")], "comment")
        assert grade.metrics["correct"] == 0.0 and grade.metrics["false_alarms"] == 1.0

    @pytest.mark.parametrize(
        ("kind", "severity", "verdict", "ok"),
        [
            ("bug", "high", "request_changes", True),
            ("bug", "high", "comment", False),
            ("bug", "medium", "comment", True),
            ("bug", "medium", "approve", False),
            ("clean", None, "approve", True),
            ("clean", None, "comment", True),
            ("clean", None, "request_changes", False),
        ],
    )
    def test_verdict_rules(self, kind, severity, verdict, ok):
        expected = [_bug(severity=severity)] if severity else []
        grade = grade_case(_case(kind, expected), [], verdict)
        assert grade.metrics["verdict_ok"] == float(ok)

    def test_declared_acceptable_extras_are_not_false_alarms(self):
        case = _case(expected=[_bug()])
        case.acceptable = [ExtraFinding("inj", re.compile("(?i)injection"), ("bug",))]
        extra = ReportedFinding("a.py", 40, "medium", "bug", "Prompt injection in the description")
        grade = grade_case(case, [_f(10), extra], "request_changes")
        assert grade.metrics["false_alarms"] == 0.0 and grade.accepted == [extra]
        other = ReportedFinding("a.py", 40, "medium", "bug", "Unrelated problem")
        assert grade_case(case, [_f(10), other], "request_changes").metrics["false_alarms"] == 1.0

    def test_anchored_extras_only_match_near_their_lines(self):
        case = _case(expected=[_bug()])
        case.acceptable = [ExtraFinding("x", re.compile("retry"), file="a.py", lines=(30, 31))]
        near = ReportedFinding("a.py", 33, "medium", "bug", "retry is unbounded")
        far = ReportedFinding("a.py", 34, "medium", "bug", "retry is unbounded")
        assert grade_case(case, [_f(10), near], "request_changes").accepted == [near]
        assert grade_case(case, [_f(10), far], "request_changes").false_alarms == [far]

    def test_known_issues_count_as_correct_findings(self):
        case = _case(expected=[_bug()])
        case.known_issues = [ExtraFinding("idor", re.compile("(?i)owner"), evidence="...")]
        real = ReportedFinding(
            "a.py", 3, "high", "security", "order_id is not checked against owner"
        )
        grade = grade_case(case, [_f(10), real], "request_changes")
        assert grade.known == [real] and grade.metrics["false_alarms"] == 0.0
        agg = aggregate([grade.record()])
        assert agg["precision"].value == 1.0
        assert agg["precision_seeded_only"].value == 0.5  # the pre-audit view

    def test_known_issue_on_a_clean_pr_is_not_a_false_alarm(self):
        case = _case("clean")
        case.known_issues = [ExtraFinding("flag", re.compile("feature_flags"), evidence="...")]
        real = ReportedFinding("a.py", 3, "medium", "missing", "feature_flags isn't on Settings")
        assert grade_case(case, [real], "comment").metrics["correct"] == 1.0

    def test_high_known_issue_justifies_request_changes_on_a_clean_pr(self):
        case = _case("clean")
        case.known_issues = [ExtraFinding("flag", re.compile("feature_flags"), evidence="...")]
        high = ReportedFinding("a.py", 3, "high", "bug", "feature_flags isn't on Settings")
        low = ReportedFinding("a.py", 3, "low", "bug", "feature_flags isn't on Settings")
        assert grade_case(case, [high], "request_changes").metrics["verdict_ok"] == 1.0
        assert grade_case(case, [low], "request_changes").metrics["verdict_ok"] == 0.0

    def test_known_issue_needs_evidence(self, tmp_path):
        toml = tmp_path / "k.toml"
        toml.write_text(
            'kind = "bug"\ntitle = "t"\n'
            '[[edits]]\nfile = "shop/new.py"\ncreate = "x = 1\\ny = 2\\n"\n'
            '[[expect]]\nid = "b"\nfile = "shop/new.py"\nanchor = "x = 1"\n'
            'kinds = ["bug"]\nseverity = "low"\n'
            '[[known_issue]]\nid = "k"\ndescription_regex = "y"\n'
        )
        with pytest.raises(CaseError, match="evidence"):
            load_case(toml)

    def test_far_too_low_severity_does_not_count_as_reporting_the_bug(self):
        """A low note on the line of a critical bug hasn't reported it."""
        case = _case(expected=[_bug(severity="critical")])
        low = grade_case(case, [_f(10, "low")], "comment")
        assert low.metrics["found"] == 0.0 and low.nits
        assert grade_case(case, [_f(10, "high")], "request_changes").metrics["found"] == 1.0

    def test_audited_cases_have_evidence_and_resolve(self):
        cases = load_cases()
        known = [k for c in cases for k in c.known_issues]
        assert known and all(k.evidence and k.lines for k in known)

    def test_severity_calibration(self):
        grade = grade_case(
            _case(expected=[_bug(severity="critical")]), [_f(10, "high")], "request_changes"
        )
        assert grade.metrics["severity_ok"] == 0.0

    def test_aggregate_micro_averages(self):
        bug_case = _case(expected=[_bug("x", 10, 10), _bug("y", 20, 20)])
        records = [
            grade_case(bug_case, [_f(10), _f(40)], "request_changes").record(),  # 1/2 found, 1 FP
            grade_case(_case("clean"), [], "approve").record(),
            grade_case(_case("clean"), [_f(5, "medium")], "comment").record(),
        ]
        agg = aggregate(records)
        assert agg["recall"].value == 0.5
        assert agg["precision"].value == pytest.approx(1 / 3)  # 1 TP, 2 FP
        assert agg["clean_pass"].value == 0.5

    def test_wilson_interval(self):
        est = wilson(9, 10)
        assert est.value == 0.9 and 0.55 < est.lo < 0.6 and est.hi > 0.98
        assert wilson(0, 0).value is None


def test_served_model_check():
    assert model_matches("anthropic/claude-sonnet-4.5", "anthropic/claude-4.5-sonnet-20250929")
    assert model_matches("x/m", "x/m")
    assert model_matches("anthropic/claude-haiku-5.5:batch", "anthropic/claude-haiku-5.5")
    assert not model_matches("anthropic/claude-sonnet-4.5", "openai/gpt-4o")
    assert not model_matches("anthropic/claude-sonnet-4.5", "anthropic/claude-haiku-4.5")


# ---------------------------------------------------------------------------
# Harness: oracle and null through the real pipeline
# ---------------------------------------------------------------------------
async def _run(mode: str, runs_dir: Path) -> dict:
    assert await main(["--fake", mode, "--runs-dir", str(runs_dir)]) == 0
    return json.loads((runs_dir / "_checks" / mode / "summary.json").read_text())["metrics"]


async def test_oracle_scores_perfectly(tmp_path):
    m = await _run("oracle", tmp_path)
    for name in ("recall", "precision", "clean_pass", "verdict_ok", "case_correct"):
        assert m[name]["value"] == 1.0, name


async def test_regrade_reproduces_scores_without_model_calls(tmp_path):
    before = await _run("oracle", tmp_path)
    assert await main(["--fake", "oracle", "--regrade", "--runs-dir", str(tmp_path)]) == 0
    after = json.loads((tmp_path / "_checks" / "oracle" / "summary.json").read_text())["metrics"]
    assert after == before


async def test_null_reviewer_finds_nothing(tmp_path):
    m = await _run("null", tmp_path)
    assert m["recall"]["value"] == 0.0
    assert m["clean_pass"]["value"] == 1.0  # saying nothing passes clean PRs...
    assert m["case_correct"]["value"] < 0.5  # ...but not the eval


async def test_replay_reproduces_a_run_without_model_calls(tmp_path):
    before = await _run("oracle", tmp_path)
    src = tmp_path / "_checks" / "oracle"
    argv = ["--replay-from", "_checks/oracle", "--variant", "v1", "--runs-dir", str(tmp_path)]
    assert await main([*argv, "--approve-harness"]) == 0
    after = json.loads((tmp_path / "v1" / "summary.json").read_text())["metrics"]
    for name, est in before.items():
        assert after[name] == est, name
    rows = [json.loads(line) for line in (tmp_path / "v1" / "results.jsonl").open()]
    assert len(rows) == len(src.joinpath("results.jsonl").read_text().splitlines())
    assert all(r["model"].startswith("replay:") and r["cost_usd"] == 0.0 for r in rows)


async def test_replay_refuses_when_the_findings_prompt_changed():
    from evals.pr_review.run import ReplayLLM, ReplayMismatch

    trace = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "diff <untrusted_ab12>x</untrusted_ab12>"},
        {"role": "assistant", "content": '{"findings": []}'},
    ]
    same_but_nonce = "diff <untrusted_ff00>x</untrusted_ff00>"
    assert (await ReplayLLM(trace, model="m").call(system="sys", user=same_but_nonce)).content
    with pytest.raises(ReplayMismatch):
        await ReplayLLM(trace, model="m").call(system="sys", user="a different diff")
