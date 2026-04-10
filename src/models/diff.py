"""ORM model for cached PR diffs."""

from datetime import datetime
from typing import Optional
from sqlalchemy import Integer, String, Text, DateTime, ForeignKey, UniqueConstraint, Index, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class DiffModel(Base):
    __tablename__ = "diffs"
    __table_args__ = (
        UniqueConstraint("repo_id", "pr_number", "head_sha", name="uq_diff_repo_pr_sha"),
        Index("ix_diff_repo_pr", "repo_id", "pr_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    diff_text: Mapped[Optional[str]] = mapped_column(Text)
    diff_size: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    repository = relationship("Repository")
