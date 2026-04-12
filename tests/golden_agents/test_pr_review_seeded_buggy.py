"""Golden test for the PR reviewer agent against seeded_buggy.

Simulates a PR that modifies ``src/buggy/db.py``:
- "Fixes" ``get_user_by_name`` to use a parameterized query (good change)
- Adds a new ``search_users`` function with SQL injection via f-string (bad change)

The reviewer should catch the new injection and NOT flag the fix as a
problem. This tests the core value proposition: the agent reviews the
*diff*, not the entire codebase.

Gated behind ``GITA_RUN_LLM_TESTS=1`` — costs one real LLM run (~$0.10).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

run_pr_review = pytest.importorskip(
    "gita.agents.pr_reviewer.recipe", reason="pr_reviewer lands in Week 4"
).run_pr_review

from gita.agents.pr_reviewer.diff_parser import (  # noqa: E402
    ChangedLineRange,
    DiffHunk,
)
from gita.github.client import PRInfo  # noqa: E402
from gita.indexer.ingest import index_repository  # noqa: E402

FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "seeded_buggy"
).resolve()


def _pr_info() -> PRInfo:
    return PRInfo(
        number=99,
        title="Fix SQL injection in get_user_by_name and add search_users",
        body=(
            "This PR fixes the SQL injection in get_user_by_name by using "
            "a parameterized query. Also adds a new search_users function "
            "for searching users by partial name match."
        ),
        author="dev-bob",
        state="open",
        base_ref="main",
        head_ref="fix/sql-injection",
        head_sha="deadbeef",
        changed_files=1,
        additions=12,
        deletions=2,
        html_url="https://github.com/test/seeded_buggy/pull/99",
    )


def _diff_hunks() -> list[DiffHunk]:
    """Simulate a PR that modifies db.py.

    The diff shows:
    1. get_user_by_name fixed (parameterized query replaces f-string)
    2. New search_users added with a NEW SQL injection via f-string

    The reviewer should flag #2 but not #1.
    """
    patch = (
        "@@ -12,7 +12,7 @@ DB_PASSWORD = \"admin123\"\n"
        " \n"
        " def get_user_by_name(conn, name):\n"
        "-    # PLANTED ISSUE: SQL injection via f-string formatting.\n"
        "-    query = f\"SELECT * FROM users WHERE name = '{name}'\"\n"
        "+    query = \"SELECT * FROM users WHERE name = ?\"\n"
        "+    return conn.execute(query, (name,)).fetchall()\n"
        "-    return conn.execute(query).fetchall()\n"
        " \n"
        "@@ -28,3 +28,12 @@ def delete_user(conn, name):\n"
        "     conn.execute(\"DELETE FROM users WHERE name = '\" + name + \"'\")\n"
        "     # Note: no conn.commit() — silent data loss on rollback.\n"
        "+\n"
        "+\n"
        "+def search_users(conn, pattern):\n"
        "+    \"\"\"Search users by partial name match.\"\"\"\n"
        "+    query = f\"SELECT * FROM users WHERE name LIKE '%{pattern}%'\"\n"
        "+    return conn.execute(query).fetchall()\n"
        "+\n"
        "+\n"
        "+def count_users(conn):\n"
        "+    return conn.execute(\"SELECT count(*) FROM users\").fetchone()[0]\n"
    )
    return [
        DiffHunk(
            file_path="src/buggy/db.py",
            status="modified",
            additions=12,
            deletions=2,
            patch=patch,
            changed_ranges=[
                ChangedLineRange(start=12, count=7),   # fix area
                ChangedLineRange(start=28, count=12),   # new functions
            ],
        )
    ]


async def test_pr_review_catches_new_injection(db_session, real_llm):
    """End-to-end: index seeded_buggy → review simulated PR → verify.

    The reviewer should find the SQL injection in the NEW search_users
    function without flagging the FIX in get_user_by_name.
    """
    assert FIXTURE.is_dir(), f"fixture missing at {FIXTURE}"

    await index_repository(db_session, "seeded_buggy", FIXTURE)
    await db_session.commit()

    result = await run_pr_review(
        db_session,
        "seeded_buggy",
        _pr_info(),
        _diff_hunks(),
        llm=real_llm,
    )

    # --- Assertions ---

    # 1. The reviewer produced at least one finding.
    assert result.findings, (
        "expected at least one finding — the PR introduces a new SQL "
        "injection in search_users. If this fails, something's wrong with "
        "the agent pipeline, not this test."
    )

    # 2. Every finding has a file:line citation (guardrails should've
    # caught any without).
    for finding in result.findings:
        assert finding.file, "finding missing file"
        assert finding.line > 0, f"finding {finding.file} has line <= 0"

    # 3. At least one finding mentions SQL injection or the new function.
    serialized = json.dumps([
        {"file": f.file, "line": f.line, "description": f.description,
         "severity": f.severity, "kind": f.kind}
        for f in result.findings
    ], indent=2).lower()
    assert any(
        term in serialized
        for term in ["sql", "injection", "search_users", "f-string", "format"]
    ), (
        f"no finding mentions SQL injection or search_users. "
        f"Findings:\n{serialized}"
    )

    # 4. Verdict is "comment" or "request_changes" — NOT "approve"
    # (the PR has a real bug).
    assert result.verdict in ("comment", "request_changes"), (
        f"expected comment or request_changes, got {result.verdict!r}"
    )

    # 5. No banned boilerplate phrases survived the guardrails.
    import re
    banned_patterns = [
        r"add unit tests",
        r"set up (?:ci|cd)",
        r"improve (?:test )?coverage",
        r"add (?:more )?documentation",
    ]
    for pattern in banned_patterns:
        for finding in result.findings:
            assert not re.search(pattern, finding.description, re.IGNORECASE), (
                f"banned phrase {pattern!r} found in finding: "
                f"{finding.description!r}"
            )

    # 6. Confidence is reasonable (above 0.3 — the comment threshold).
    assert result.confidence >= 0.3, (
        f"confidence {result.confidence:.2f} is below the comment threshold"
    )
