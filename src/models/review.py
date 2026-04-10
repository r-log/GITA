"""ORM model for PR review records."""

from datetime import datetime
from typing import Optional
from sqlalchemy import BigInteger, Integer, String, Text, DateTime, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class ReviewModel(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        Index("ix_review_repo_pr", "repo_id", "pr_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    author_login: Mapped[Optional[str]] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(30), nullable=False)  # approved, changes_requested, commented, dismissed
    body: Mapped[Optional[str]] = mapped_column(Text)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    repository = relationship("Repository")
