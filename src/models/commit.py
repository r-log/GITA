"""ORM model for commit records extracted from push events."""

from datetime import datetime
from typing import Optional
from sqlalchemy import Integer, String, Text, DateTime, ForeignKey, UniqueConstraint, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class CommitModel(Base):
    __tablename__ = "commits"
    __table_args__ = (
        UniqueConstraint("repo_id", "sha"),
        Index("ix_commit_repo_author", "repo_id", "author_login"),
        Index("ix_commit_committed_at", "repo_id", "committed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    sha: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    author_name: Mapped[Optional[str]] = mapped_column(String(200))
    author_email: Mapped[Optional[str]] = mapped_column(String(200))
    author_login: Mapped[Optional[str]] = mapped_column(String(100))
    committed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    files_added: Mapped[dict] = mapped_column(JSONB, default=list)
    files_modified: Mapped[dict] = mapped_column(JSONB, default=list)
    files_removed: Mapped[dict] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    repository = relationship("Repository")
