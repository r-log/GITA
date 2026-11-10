"""A small repository with a known history, built with real git commands."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.history.repo_builder import RepoBuilder

CART_V0 = "def total(prices):\n    return sum(prices)\n"
CART_V1 = CART_V0 + "\n\ndef discounted(price, percent):\n    return price * percent / 100\n"
CART_V2 = CART_V1.replace("price * percent / 100", "price * (100 - percent) / 100")


@pytest.fixture
def shop_repo(tmp_path: Path) -> RepoBuilder:
    """main: an inherited upstream squash commit "(#1)", then

    #1 feat/discount   adds discounted() with a bug (line 6)
    #2 docs/readme     README only
    #3 fix/discount    fixes line 6                      -> introduced by #1
    #4 (squash) fix:   rewrites total()'s return line   -> code older than any PR
    #5 fix/test-typo   touches only tests/              -> nothing to trace
    """
    (tmp_path / "shop").mkdir()
    r = RepoBuilder(tmp_path / "shop")
    r.commit(
        "Upstream: initial cart (#1)", src__shop__cart_py=CART_V0, tests__test_cart_py="x = 1\n"
    )
    r.pr(1, "feat/discount", "Add discounts", src__shop__cart_py=CART_V1)
    r.pr(2, "docs/readme", "Document the cart", README_md="# Shop\n")
    r.pr(3, "fix/discount", "Discount subtracts the percentage", src__shop__cart_py=CART_V2)
    r.commit(
        "fix: empty cart totals 0 (#4)",
        src__shop__cart_py=CART_V2.replace(
            "return sum(prices)", "return sum(prices) if prices else 0"
        ),
    )
    r.pr(5, "fix/test-typo", "Fix a test typo", tests__test_cart_py="x = 2\n")
    return r
