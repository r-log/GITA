"""Tests for the test-generator bridge (Week 8 Day 3).

The bridge is pure: given an artifact, return the three Decisions that
the framework will route (create_branch → update_file → open_pr). All
tests operate on in-memory artifacts; no DB, no network, no LLM.
"""
from __future__ import annotations

import hashlib

from gita.agents.decisions import Decision
from gita.agents.dedupe import compute_signature
from gita.agents.test_generator.bridge import (
    TestGenerationArtifact,
    build_test_generation_decisions,
    compute_branch_name,
    default_pr_title,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _artifact(
    *,
    repo: str = "owner/repo",
    base_branch: str = "main",
    base_sha: str = "108125a8b7b512b99cf95af3565b7e2351275bb0",
    target_file: str = "backend/app/utils/decorators.py",
    test_file_path: str = "tests/test_decorators.py",
    test_content: str = "def test_ok():\n    assert True\n",
    existing_test_sha: str | None = None,
    pr_title: str | None = None,
    pr_body: str | None = None,
    fallback_issue: int | None = None,
    confidence: float = 0.9,
) -> TestGenerationArtifact:
    return TestGenerationArtifact(
        repo=repo,
        base_branch=base_branch,
        base_sha=base_sha,
        target_file=target_file,
        test_file_path=test_file_path,
        test_content=test_content,
        existing_test_sha=existing_test_sha,
        pr_title=pr_title,
        pr_body=pr_body,
        fallback_issue=fallback_issue,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Branch naming — deterministic and dedupe-stable
# ---------------------------------------------------------------------------
class TestBranchName:
    def test_strips_py_extension(self):
        name = compute_branch_name("foo/bar.py", "abc1234defgh")
        assert name == "gita/tests/foo-bar-abc1234"

    def test_collapses_non_alnum_runs(self):
        name = compute_branch_name(
            "backend/app/utils/_private.py", "deadbeefcafe"
        )
        # leading/trailing hyphens stripped, runs collapsed
        assert name == "gita/tests/backend-app-utils-private-deadbee"

    def test_deterministic_same_inputs(self):
        a = compute_branch_name("a/b.py", "123456789abc")
        b = compute_branch_name("a/b.py", "123456789abc")
        assert a == b

    def test_sha_prefix_shortens_to_7(self):
        name = compute_branch_name(
            "foo.py", "abcdef0123456789abcdef0123456789"
        )
        assert name.endswith("-abcdef0")

    def test_short_sha_reused_as_is(self):
        """If the caller passes a short SHA (e.g. from a log), don't
        pad or crash — just use what we've got."""
        name = compute_branch_name("foo.py", "abc123")
        assert name == "gita/tests/foo-abc123"

    def test_empty_sha_falls_back_to_unknown(self):
        name = compute_branch_name("foo.py", "")
        assert name == "gita/tests/foo-unknown"


# ---------------------------------------------------------------------------
# build_test_generation_decisions — shape + ordering
# ---------------------------------------------------------------------------
class TestDecisionListShape:
    def test_returns_three_decisions_in_order(self):
        decisions = build_test_generation_decisions(_artifact())
        assert len(decisions) == 3
        assert [d.action for d in decisions] == [
            "create_branch",
            "update_file",
            "open_pr",
        ]

    def test_all_share_repo_and_confidence(self):
        decisions = build_test_generation_decisions(
            _artifact(confidence=0.93)
        )
        for d in decisions:
            assert d.target["repo"] == "owner/repo"
            assert d.confidence == 0.93

    def test_all_decisions_isolated_targets(self):
        """Target dicts must be distinct — mutating one Decision's
        target must not bleed into the others."""
        decisions = build_test_generation_decisions(_artifact())
        decisions[0].target["mutated"] = True
        assert "mutated" not in decisions[1].target
        assert "mutated" not in decisions[2].target


class TestCreateBranchDecision:
    def test_payload_matches_expected_branch_and_sha(self):
        art = _artifact(base_sha="108125a8b7b512b99cf95af3565b7e2351275bb0")
        [create_branch, _, _] = build_test_generation_decisions(art)
        assert create_branch.payload["ref"] == (
            "refs/heads/gita/tests/"
            "backend-app-utils-decorators-108125a"
        )
        assert create_branch.payload["base_sha"] == art.base_sha

    def test_evidence_is_flat_one_liners(self):
        """The framework's downgrade render bullets each evidence line;
        embedding code fences would break that. Evidence must stay as
        self-contained one-liners."""
        decisions = build_test_generation_decisions(_artifact())
        for d in decisions:
            for ev in d.evidence:
                assert "```" not in ev, (
                    f"evidence for {d.action} contains a code fence; "
                    "that breaks the bullet render in "
                    "_render_downgrade_body"
                )


class TestUpdateFileDecision:
    def test_payload_has_path_content_message_branch(self):
        art = _artifact()
        [_, update_file, _] = build_test_generation_decisions(art)
        assert update_file.payload["path"] == "tests/test_decorators.py"
        assert update_file.payload["content"] == art.test_content
        assert (
            update_file.payload["message"]
            == "gita: add generated tests for backend/app/utils/decorators.py"
        )
        assert update_file.payload["branch"] == (
            "gita/tests/backend-app-utils-decorators-108125a"
        )

    def test_create_path_has_no_sha(self):
        [_, update_file, _] = build_test_generation_decisions(_artifact())
        assert "sha" not in update_file.payload

    def test_update_path_includes_existing_blob_sha(self):
        art = _artifact(existing_test_sha="oldblobsha1234")
        [_, update_file, _] = build_test_generation_decisions(art)
        assert update_file.payload["sha"] == "oldblobsha1234"

    def test_evidence_mentions_size_and_hash(self):
        art = _artifact(test_content="abc\n")
        [_, update_file, _] = build_test_generation_decisions(art)
        assert any("4 bytes" in ev for ev in update_file.evidence)
        expected_hash_prefix = hashlib.sha256(b"abc\n").hexdigest()[:12]
        assert any(
            expected_hash_prefix in ev for ev in update_file.evidence
        )


class TestOpenPrDecision:
    def test_payload_has_title_body_head_base(self):
        art = _artifact()
        [_, _, open_pr] = build_test_generation_decisions(art)
        assert open_pr.payload["head"] == (
            "gita/tests/backend-app-utils-decorators-108125a"
        )
        assert open_pr.payload["base"] == "main"
        assert open_pr.payload["title"] == default_pr_title(
            art.target_file
        )
        assert art.target_file in open_pr.payload["body"]
        assert art.test_file_path in open_pr.payload["body"]

    def test_custom_title_and_body_override_defaults(self):
        art = _artifact(
            pr_title="My bespoke title",
            pr_body="My bespoke body.",
        )
        [_, _, open_pr] = build_test_generation_decisions(art)
        assert open_pr.payload["title"] == "My bespoke title"
        assert open_pr.payload["body"] == "My bespoke body."


# ---------------------------------------------------------------------------
# Fallback-issue plumbing — required for COMMENT-mode downgrade
# ---------------------------------------------------------------------------
class TestFallbackIssue:
    def test_omitted_when_not_set(self):
        decisions = build_test_generation_decisions(_artifact())
        for d in decisions:
            assert "fallback_issue" not in d.target

    def test_present_on_all_three_decisions(self):
        decisions = build_test_generation_decisions(
            _artifact(fallback_issue=42)
        )
        for d in decisions:
            assert d.target["fallback_issue"] == 42


# ---------------------------------------------------------------------------
# Integration with the dedupe signatures from Day 2
# ---------------------------------------------------------------------------
class TestDedupeSignaturesAreStable:
    def test_same_artifact_produces_same_signatures(self):
        """Two identical artifacts hash each of their 3 Decisions to
        the same signature — the mechanism that lets a retried
        test-gen run resume where the last one left off."""
        a = build_test_generation_decisions(_artifact())
        b = build_test_generation_decisions(_artifact())
        for da, db in zip(a, b):
            assert compute_signature(da) == compute_signature(db)

    def test_different_base_sha_changes_all_three_signatures(self):
        a = build_test_generation_decisions(
            _artifact(base_sha="aaaa1111aaaa1111")
        )
        b = build_test_generation_decisions(
            _artifact(base_sha="bbbb2222bbbb2222")
        )
        for da, db in zip(a, b):
            assert compute_signature(da) != compute_signature(db), (
                f"same signature for {da.action} across different SHAs"
            )

    def test_different_content_changes_update_file_only(self):
        """Branch-off and PR-open are identified by branch pair and
        base SHA, which don't depend on the test body. Only the
        update_file signature should change when the generated test
        content changes."""
        a = build_test_generation_decisions(
            _artifact(test_content="def test_a(): ...\n")
        )
        b = build_test_generation_decisions(
            _artifact(test_content="def test_b(): ...\n")
        )
        assert compute_signature(a[0]) == compute_signature(b[0])
        assert compute_signature(a[1]) != compute_signature(b[1])
        assert compute_signature(a[2]) == compute_signature(b[2])


# ---------------------------------------------------------------------------
# Downgrade render — code-action preview lands cleanly
# ---------------------------------------------------------------------------
class TestDowngradeRendersPreview:
    """Confirms the bridge + _render_downgrade_body round-trip produces
    a readable comment body under COMMENT mode."""

    def test_update_file_downgrade_body_has_code_block(self):
        from gita.agents.decisions import _render_downgrade_body

        [_, update_file, _] = build_test_generation_decisions(
            _artifact(test_content="def test_x():\n    assert 1 == 1\n")
        )
        body = _render_downgrade_body(update_file, reason="shadow-test")
        assert "**Proposed content of `tests/test_decorators.py`:**" in body
        assert "```python" in body
        assert "def test_x():" in body

    def test_update_file_downgrade_truncates_huge_content(self):
        from gita.agents.decisions import _render_downgrade_body

        huge = "\n".join(f"line_{i}" for i in range(200))
        [_, update_file, _] = build_test_generation_decisions(
            _artifact(test_content=huge)
        )
        body = _render_downgrade_body(update_file, reason="too big")
        assert "more lines)" in body

    def test_open_pr_downgrade_body_has_branch_pair(self):
        from gita.agents.decisions import _render_downgrade_body

        [_, _, open_pr] = build_test_generation_decisions(_artifact())
        body = _render_downgrade_body(open_pr, reason="shadow")
        assert "**Proposed PR:**" in body
        assert "head: " in body
        assert "base: `main`" in body

    def test_create_branch_downgrade_body_has_ref(self):
        from gita.agents.decisions import _render_downgrade_body

        [create_branch, _, _] = build_test_generation_decisions(_artifact())
        body = _render_downgrade_body(create_branch, reason="shadow")
        assert "**Proposed branch:**" in body
        assert "refs/heads/gita/tests/" in body
