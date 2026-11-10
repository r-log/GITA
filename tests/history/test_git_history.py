"""Reading merged PRs and tracing fixes, on a repository with a known history."""

from __future__ import annotations

from gita.history.git_history import classify, is_product_path, link_fix, list_merged_prs


def _link(repo, number):
    prs = {p.number: p for p in list_merged_prs(repo.root)}
    owner = {c: p.number for p in reversed(list(prs.values())) for c in p.commits}
    return link_fix(
        repo.root,
        prs[number],
        lambda commits: {c: owner[c] for c in commits if c in owner},
        lambda n: prs[n].head_sha if n in prs else None,
    )


def test_merged_prs_with_kinds_and_both_merge_styles(shop_repo):
    prs = list_merged_prs(shop_repo.root)
    assert [p.number for p in prs] == [1, 2, 3, 4, 5]
    assert [p.kind for p in prs] == ["feature", "docs", "fix", "fix", "fix"]
    squash = prs[3]
    assert squash.branch is None and squash.title == "fix: empty cart totals 0"
    assert prs[0].title == "Add discounts" and prs[0].branch == "feat/discount"


def test_inherited_squash_commit_is_not_a_pr_of_this_repo(shop_repo):
    """The upstream "(#1)" predates the first merge commit: #1 is the merge PR."""
    first = list_merged_prs(shop_repo.root)[0]
    assert first.branch == "feat/discount"


def test_pr_files_are_its_own_changes(shop_repo):
    docs = list_merged_prs(shop_repo.root)[1]
    assert [(f.path, f.additions, f.deletions) for f in docs.files] == [("README.md", 1, 0)]


def test_a_fix_traces_to_the_pr_that_wrote_the_line(shop_repo):
    links = _link(shop_repo, 3)
    assert len(links) == 1
    link = links[0]
    assert (link.introducing_pr, link.share) == (1, 1.0)
    assert link.lines == [{"file": "src/shop/cart.py", "lines": [6]}]


def test_code_older_than_any_pr_is_inherited(shop_repo):
    (link,) = _link(shop_repo, 4)
    assert link.introducing_pr is None and link.share == 1.0


def test_a_fix_to_tests_only_traces_to_nothing(shop_repo):
    assert _link(shop_repo, 5) == []


def test_classify():
    assert classify("fix/discount", "x") == "fix"
    assert classify("feat/a", "x") == "feature"
    assert classify(None, "fix(cart): empty totals") == "fix"
    assert classify(None, "[refactor] split cart") == "refactor"
    assert classify("someone/patch-1", "Update cart") == "other"
    assert classify(None, "[Quests] Auto-accept quests") == "other"  # an area tag


def test_product_paths():
    assert is_product_path("src/shop/cart.py") and is_product_path("src/game/Player.cpp")
    assert not is_product_path("tests/test_cart.py") and not is_product_path("README.md")
