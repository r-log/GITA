"""
Async database engine and session factory.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from src.core.config import settings

# Create async engine.
# SQLAlchemy echo is OFF even in dev — it generates ~3 log lines per query,
# which drowns out actual agent telemetry during long operations like the
# graph builder's per-node inserts. Re-enable with DB_ECHO=1 env var if you
# specifically need to debug SQL.
import os
engine = create_async_engine(
    settings.database_url,
    echo=os.getenv("DB_ECHO") == "1",
    pool_size=10,
    max_overflow=20,
)

# Session factory
async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Base class for all ORM models."""
    pass


async def get_db() -> AsyncSession:
    """Dependency for FastAPI routes."""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
