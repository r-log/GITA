"""ORM model for PR analysis records."""

from datetime import datetime
from typing import Optional
from sqlalchemy import Integer, String, Text, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class PullRequestModel(Base):
    __tablename__ = "pull_requests"
    __table_args__ = (UniqueConstraint("repo_id", "github_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    github_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(255))
    state: Mapped[Optional[str]] = mapped_column(String(20))
    author: Mapped[Optional[str]] = mapped_column(String(100))
    linked_issue_numbers: Mapped[dict] = mapped_column(JSONB, default=list)
    diff_size: Mapped[Optional[int]] = mapped_column(Integer)
    files_changed: Mapped[Optional[int]] = mapped_column(Integer)
    body: Mapped[Optional[str]] = mapped_column(Text)
    base_branch: Mapped[Optional[str]] = mapped_column(String(200))
    head_branch: Mapped[Optional[str]] = mapped_column(String(200))
    github_created_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    merged_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    merged_by: Mapped[Optional[str]] = mapped_column(String(100))
    commit_count: Mapped[Optional[int]] = mapped_column(Integer)
    risk_level: Mapped[Optional[str]] = mapped_column(String(20))  # info, warning, critical
    last_analyzed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    repository = relationship("Repository", back_populates="pull_requests")
