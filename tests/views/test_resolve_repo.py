"""Tests for resolve_repo with github_full_name fallback.

Verifies the two-step lookup: short name first, then case-insensitive
github_full_name. Webhook-triggered jobs depend on the fallback path.
"""
import pytest

from gita.db.models import Repo
from gita.views._common import RepoNotFoundError, resolve_repo


class TestResolveByShortName:
    """Existing behaviour — resolve by Repo.name (fast path)."""

    async def test_exact_short_name(self, db_session):
        db_session.add(Repo(name="amass", root_path="/tmp/amass"))
        await db_session.flush()

        repo = await resolve_repo(db_session, "amass")
        assert repo.name == "amass"

    async def test_short_name_takes_precedence_over_full_name(self, db_session):
        """If a repo's short name matches the query, return it even when
        another repo has a matching github_full_name."""
        db_session.add(Repo(name="amass", root_path="/tmp/amass"))
        db_session.add(
            Repo(
                name="other",
                root_path="/tmp/other",
                github_full_name="amass",
            )
        )
        await db_session.flush()

        repo = await resolve_repo(db_session, "amass")
        assert repo.name == "amass"


class TestResolveByGithubFullName:
    """Fallback path — resolve by Repo.github_full_name."""

    async def test_resolve_by_github_full_name(self, db_session):
        db_session.add(
            Repo(
                name="amass",
                root_path="/tmp/amass",
                github_full_name="r-log/AMASS",
            )
        )
        await db_session.flush()

        repo = await resolve_repo(db_session, "r-log/AMASS")
        assert repo.name == "amass"
        assert repo.github_full_name == "r-log/AMASS"

    async def test_case_insensitive_github_full_name(self, db_session):
        db_session.add(
            Repo(
                name="amass",
                root_path="/tmp/amass",
                github_full_name="r-log/AMASS",
            )
        )
        await db_session.flush()

        repo = await resolve_repo(db_session, "R-LOG/amass")
        assert repo.name == "amass"

    async def test_github_full_name_with_whitespace(self, db_session):
        db_session.add(
            Repo(
                name="amass",
                root_path="/tmp/amass",
                github_full_name="r-log/AMASS",
            )
        )
        await db_session.flush()

        repo = await resolve_repo(db_session, "  r-log/AMASS  ")
        assert repo.name == "amass"


class TestResolveNotFound:

    async def test_not_found_raises(self, db_session):
        with pytest.raises(RepoNotFoundError, match="not-a-real-repo"):
            await resolve_repo(db_session, "not-a-real-repo")

    async def test_no_github_full_name_set(self, db_session):
        """Repo with only a short name can't be found by full name."""
        db_session.add(Repo(name="amass", root_path="/tmp/amass"))
        await db_session.flush()

        with pytest.raises(RepoNotFoundError):
            await resolve_repo(db_session, "r-log/AMASS")
