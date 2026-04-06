"""ORM model for PR file changes — tracks which files each PR actually changed."""

from datetime import datetime
from sqlalchemy import Integer, String, DateTime, ForeignKey, UniqueConstraint, Index, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class PrFileChange(Base):
    __tablename__ = "pr_file_changes"
    __table_args__ = (
        UniqueConstraint("pr_id", "file_path", name="uq_pr_file_change_pr_file"),
        Index("ix_pr_file_change_repo_file", "repo_id", "file_path"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    pr_id: Mapped[int] = mapped_column(ForeignKey("pull_requests.id"))
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    change_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # added, modified, removed, renamed
    additions: Mapped[int] = mapped_column(Integer, default=0)
    deletions: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    repository = relationship("Repository")
    pull_request = relationship("PullRequestModel")
