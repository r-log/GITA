"""The backtest's grader and case loading (no clone needed)."""

from __future__ import annotations

from evals.pr_backtest.run import BacktestCase, Bug, added_lines, grade, load_cases


def _case() -> BacktestCase:
    case = BacktestCase(id="pr1", pr=1, title="t", base="b", head="h")
    case.bugs = [
        Bug("fixA", 2, "t", "code", "introduced", "", {"src/a.cpp": {40, 41}}),
        Bug("fixB", 3, "t", "domain", "moved", "", {"src/b.cpp": {10}}),
    ]
    return case


FILES = [
    {"filename": "src/a.cpp", "patch": "@@ -30,2 +30,12 @@\n" + "+x\n" * 12},
    {"filename": "src/b.cpp", "patch": "@@ -1,0 +1,20 @@\n" + "+y\n" * 20},
]


def test_added_lines_follow_hunk_headers():
    added = added_lines(FILES)
    assert added["src/a.cpp"] == set(range(30, 42))
    assert added["src/b.cpp"] == set(range(1, 21))


def test_a_finding_locates_within_tolerance_only():
    near = {"file": "src/a.cpp", "line": 37}  # 3 from 40
    far = {"file": "src/a.cpp", "line": 36}
    graded = grade(_case(), [near, far], FILES, {"src/a.cpp", "src/b.cpp"})
    a = graded["per_bug"][0]
    assert a["located"] and a["hits"] == [0]
    assert graded["metrics"]["found"] == 0.5 and graded["metrics"]["found_code"] == 1.0


def test_chance_grows_with_the_number_of_findings():
    one = grade(_case(), [{"file": "src/x.cpp", "line": 1}], FILES, set())
    many = grade(_case(), [{"file": "src/x.cpp", "line": 1}] * 5, FILES, set())
    assert 0 < one["metrics"]["chance"] < many["metrics"]["chance"] < 1


def test_every_mined_defect_is_labelled_and_excluded_ones_are_dropped():
    cases = load_cases()  # raises if a mined defect has no label
    visible = {b.visible for c in cases for b in c.bugs}
    assert visible <= {"code", "repo", "domain"}
    assert all(c.bugs for c in cases)
