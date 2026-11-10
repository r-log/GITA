"""Syncing the history memory into Postgres: full, incremental, idempotent."""

from __future__ import annotations

from sqlalchemy import select

from gita.db.models import DefectLink, HistoryPR, HistoryPRFile
from gita.history.sync import sync_history


async def test_full_then_noop_then_incremental(db_session, shop_repo):
    first = await sync_history(db_session, "shop", shop_repo.root)
    assert (first.mode, first.prs_added, first.fixes_linked, first.links_added) == ("full", 5, 3, 2)

    again = await sync_history(db_session, "shop", shop_repo.root)
    assert (again.mode, again.prs_added) == ("noop", 0)

    shop_repo.pr(6, "feat/coupons", "Add coupons", src__shop__coupons_py="def apply():\n    pass\n")
    more = await sync_history(db_session, "shop", shop_repo.root)
    assert (more.mode, more.prs_added, more.fixes_linked) == ("incremental", 1, 0)
    numbers = (await db_session.execute(select(HistoryPR.number))).scalars().all()
    assert sorted(numbers) == [1, 2, 3, 4, 5, 6]


async def test_links_and_files_are_stored(db_session, shop_repo):
    await sync_history(db_session, "shop", shop_repo.root)
    links = {
        (link.fix_pr, link.introducing_pr): link
        for link in (await db_session.execute(select(DefectLink))).scalars()
    }
    assert set(links) == {(3, 1), (4, None)}
    assert links[(3, 1)].lines == [{"file": "src/shop/cart.py", "lines": [6]}]
    readme = (
        await db_session.execute(select(HistoryPRFile).where(HistoryPRFile.pr_number == 2))
    ).scalar_one()
    assert readme.path == "README.md"


async def test_rewritten_history_starts_over(db_session, shop_repo):
    await sync_history(db_session, "shop", shop_repo.root)
    shop_repo.git("reset", "-q", "--hard", "HEAD~1")  # drop #5 from main
    shop_repo.pr(
        7, "fix/other", "Another fix", src__shop__cart_py="def total(prices):\n    return 0\n"
    )
    redo = await sync_history(db_session, "shop", shop_repo.root)
    assert redo.mode == "full"
    numbers = (await db_session.execute(select(HistoryPR.number))).scalars().all()
    assert sorted(numbers) == [1, 2, 3, 4, 7]
