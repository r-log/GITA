"""
Repository upsert — ensures a repo record exists in the DB for every webhook.
Returns the repo_id for use by agents and DB tools.
"""

from sqlalchemy import select
from src.core.database import async_session
from src.models.repository import Repository


async def upsert_repository(github_id: int, full_name: str, installation_id: int) -> int:
    """
    Ensure a repository record exists. Returns the DB repo_id.
    Creates if new, updates installation_id if changed.
    """
    async with async_session() as session:
        stmt = select(Repository).where(Repository.github_id == github_id)
        result = await session.execute(stmt)
        repo = result.scalar_one_or_none()

        if repo:
            if repo.installation_id != installation_id or repo.full_name != full_name:
                repo.installation_id = installation_id
                repo.full_name = full_name
                await session.commit()
            return repo.id

        repo = Repository(
            github_id=github_id,
            full_name=full_name,
            installation_id=installation_id,
        )
        session.add(repo)
        await session.commit()
        await session.refresh(repo)
        return repo.id
